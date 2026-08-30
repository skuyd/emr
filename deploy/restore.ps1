[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EncryptedBackup,
    [Parameter(Mandatory = $true)]
    [string]$LatestTombstoneBackup,
    [Parameter(Mandatory = $true)]
    [string]$AgeIdentityFile,
    [Parameter(Mandatory = $true)]
    [string]$WorkRoot,
    [Parameter(Mandatory = $true)]
    [string]$RestoreDatabase,
    [Parameter(Mandatory = $true)]
    [string]$RestorePrefix,
    [Parameter(Mandatory = $true)]
    [ValidateSet("RESTORE-DRILL")]
    [string]$Confirmation
)

$ErrorActionPreference = "Stop"
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$environmentFile = Join-Path $repositoryRoot ".env.production"
$composeFile = Join-Path $PSScriptRoot "compose.yaml"

function Resolve-SafeRoot([string]$PathValue) {
    $absolute = [IO.Path]::GetFullPath($PathValue)
    $volumeRoot = [IO.Path]::GetPathRoot($absolute)
    if ($absolute.TrimEnd('\', '/') -eq $volumeRoot.TrimEnd('\', '/')) {
        throw "WorkRoot must not be a filesystem root."
    }
    New-Item -ItemType Directory -Path $absolute -Force | Out-Null
    return (Get-Item -LiteralPath $absolute).FullName
}

function Assert-ChildPath([string]$Root, [string]$Candidate) {
    $rootPrefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $absoluteCandidate = [IO.Path]::GetFullPath($Candidate)
    if (-not $absoluteCandidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside WorkRoot."
    }
    return $absoluteCandidate
}

function Get-BackupTimestamp([string]$BackupPath) {
    $name = [IO.Path]::GetFileName($BackupPath)
    if ($name -notmatch '^phr-backup-(\d{8}T\d{6}Z)\.tar\.gz\.age$') {
        throw "Backup filename must retain the generated UTC timestamp."
    }
    return [DateTime]::ParseExact(
        $Matches[1],
        "yyyyMMddTHHmmssZ",
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal
    )
}

function Expand-VerifiedBackup(
    [string]$Encrypted,
    [string]$Identity,
    [string]$ArchivePath,
    [string]$Destination,
    [string]$SafeRoot,
    [string]$ExpectedBackupName
) {
    & age --decrypt --identity $Identity --output $ArchivePath $Encrypted
    if ($LASTEXITCODE -ne 0) { throw "Backup decryption failed." }

    $archiveEntries = @(& tar -tzf $ArchivePath)
    if ($LASTEXITCODE -ne 0 -or $archiveEntries.Count -eq 0) {
        throw "Backup archive listing failed."
    }
    $verboseEntries = @(& tar -tvzf $ArchivePath)
    if ($LASTEXITCODE -ne 0 -or $verboseEntries.Count -eq 0) {
        throw "Backup archive type listing failed."
    }
    foreach ($listing in $verboseEntries) {
        if (-not $listing -or $listing[0] -notin ('-', 'd')) {
            throw "Backup archive must contain only regular files and directories."
        }
    }
    foreach ($entry in $archiveEntries) {
        $normalized = [string]$entry
        if (
            [string]::IsNullOrWhiteSpace($normalized) -or
            $normalized.StartsWith('/') -or
            $normalized.Contains('\') -or
            $normalized -match '^[a-zA-Z]:' -or
            ($normalized.ToCharArray() | Where-Object { [char]::IsControl($_) })
        ) {
            throw "Backup archive contains an unsafe path."
        }
        $segments = @($normalized.TrimEnd('/').Split('/') | Where-Object { $_ -ne '' })
        if (
            $segments.Count -eq 0 -or
            $segments[0] -cne $ExpectedBackupName -or
            $segments -contains '.' -or
            $segments -contains '..'
        ) {
            throw "Backup archive is not rooted in the expected snapshot directory."
        }
    }
    & tar -C $Destination -xzf $ArchivePath --strip-components=1
    if ($LASTEXITCODE -ne 0) { throw "Backup extraction failed." }

    if (Get-ChildItem -LiteralPath $Destination -Recurse -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue) {
        throw "Backup extraction produced a link or reparse point."
    }

    $manifest = Join-Path $Destination "manifest.sha256"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "Backup manifest is missing." }
    $expectedFiles = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($line in [IO.File]::ReadAllLines($manifest)) {
        if ($line -notmatch '^([0-9a-f]{64})  ([a-zA-Z0-9._/-]+)$') { throw "Backup manifest is invalid." }
        $relative = $Matches[2]
        $relativeSegments = @($relative.Split('/'))
        if (
            $relative -eq "manifest.sha256" -or
            $relativeSegments -contains '.' -or
            $relativeSegments -contains '..' -or
            -not $expectedFiles.Add($relative)
        ) {
            throw "Backup manifest contains an unsafe or duplicate path."
        }
        $candidate = Assert-ChildPath $Destination (Join-Path $Destination $relative)
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "Backup artifact is missing." }
        $actual = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $Matches[1]) { throw "Backup checksum mismatch." }
    }
    foreach ($required in ("backup-metadata.json", "database.dump", "deletion-tombstones.jsonl")) {
        if (-not $expectedFiles.Contains($required)) { throw "Backup manifest is incomplete." }
    }
    $actualFiles = @(
        Get-ChildItem -LiteralPath $Destination -Recurse -Force -File |
            Where-Object { $_.FullName -ne $manifest }
    )
    if ($actualFiles.Count -ne $expectedFiles.Count) { throw "Backup contains unmanifested artifacts." }
    foreach ($file in $actualFiles) {
        $relative = $file.FullName.Substring($Destination.Length + 1).Replace('\', '/')
        if (-not $expectedFiles.Contains($relative)) { throw "Backup contains an unmanifested artifact." }
    }
}

function Get-VerifiedBackupMetadata([string]$Destination) {
    $metadataPath = Join-Path $Destination "backup-metadata.json"
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
        throw "Backup metadata is missing."
    }
    try {
        $metadata = [IO.File]::ReadAllText($metadataPath) | ConvertFrom-Json
        $createdAt = [DateTime]::Parse(
            [string]$metadata.created_at_utc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
    }
    catch {
        throw "Backup metadata is invalid."
    }
    if (
        $metadata.schema_version -ne 1 -or
        -not $metadata.backup_name -or
        -not ($metadata.includes -contains "database") -or
        -not ($metadata.includes -contains "objects") -or
        -not ($metadata.includes -contains "deletion-tombstones")
    ) {
        throw "Backup metadata is incomplete."
    }
    return [pscustomobject]@{ Name = [string]$metadata.backup_name; CreatedAt = $createdAt }
}

if ($RestoreDatabase -notmatch '^[a-zA-Z0-9_]+_restore_drill$') {
    throw "RestoreDatabase must be an isolated *_restore_drill database."
}
if ($RestorePrefix -notmatch '^restore-drill/[a-zA-Z0-9._-]+$') {
    throw "RestorePrefix must be an isolated restore-drill/* prefix."
}
foreach ($file in ($EncryptedBackup, $LatestTombstoneBackup, $AgeIdentityFile, $environmentFile)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required file is missing." }
}
foreach ($command in ("docker", "age", "tar")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command is unavailable: $command"
    }
}
$dataBackupTime = Get-BackupTimestamp $EncryptedBackup
$tombstoneBackupTime = Get-BackupTimestamp $LatestTombstoneBackup
$dataBackupName = [IO.Path]::GetFileName($EncryptedBackup) -replace '\.tar\.gz\.age$', ''
$tombstoneBackupName = [IO.Path]::GetFileName($LatestTombstoneBackup) -replace '\.tar\.gz\.age$', ''
if ($tombstoneBackupTime -lt $dataBackupTime) {
    throw "LatestTombstoneBackup must be the same age or newer than EncryptedBackup."
}

$safeRoot = Resolve-SafeRoot $WorkRoot
$restoreName = "restore-" + [Guid]::NewGuid().ToString("N")
$restoreRoot = Assert-ChildPath $safeRoot (Join-Path $safeRoot $restoreName)
$archive = Assert-ChildPath $safeRoot (Join-Path $safeRoot ($restoreName + ".tar.gz"))
$tombstoneRestoreName = "tombstones-" + [Guid]::NewGuid().ToString("N")
$tombstoneRoot = Assert-ChildPath $safeRoot (Join-Path $safeRoot $tombstoneRestoreName)
$tombstoneArchive = Assert-ChildPath $safeRoot (Join-Path $safeRoot ($tombstoneRestoreName + ".tar.gz"))
New-Item -ItemType Directory -Path $restoreRoot | Out-Null
New-Item -ItemType Directory -Path $tombstoneRoot | Out-Null
$started = [DateTime]::UtcNow
$previousRestoreRoot = $env:RESTORE_ROOT
$previousRestoreDatabase = $env:RESTORE_DATABASE
$previousRestorePrefix = $env:RESTORE_PREFIX
try {
    Expand-VerifiedBackup $EncryptedBackup $AgeIdentityFile $archive $restoreRoot $safeRoot $dataBackupName
    Expand-VerifiedBackup $LatestTombstoneBackup $AgeIdentityFile $tombstoneArchive $tombstoneRoot $safeRoot $tombstoneBackupName
    $dataMetadata = Get-VerifiedBackupMetadata $restoreRoot
    $tombstoneMetadata = Get-VerifiedBackupMetadata $tombstoneRoot
    if ($dataMetadata.Name -cne $dataBackupName -or $tombstoneMetadata.Name -cne $tombstoneBackupName) {
        throw "Backup metadata identity does not match its encrypted archive."
    }
    if ($tombstoneMetadata.CreatedAt -lt $dataMetadata.CreatedAt) {
        throw "Latest tombstone metadata is older than the restored data snapshot."
    }
    $latestTombstones = Assert-ChildPath $tombstoneRoot (Join-Path $tombstoneRoot "deletion-tombstones.jsonl")
    if (-not (Test-Path -LiteralPath $latestTombstones -PathType Leaf)) {
        throw "Latest deletion tombstone log is missing."
    }
    $restoreTombstones = Assert-ChildPath $restoreRoot (Join-Path $restoreRoot "deletion-tombstones.jsonl")
    Copy-Item -LiteralPath $latestTombstones -Destination $restoreTombstones -Force

    $env:RESTORE_ROOT = $restoreRoot
    $env:RESTORE_DATABASE = $RestoreDatabase
    $env:RESTORE_PREFIX = $RestorePrefix
    $compose = @("compose", "--env-file", $environmentFile, "--file", $composeFile, "--profile", "tools")
    & docker @compose run --rm db-restore-drill
    if ($LASTEXITCODE -ne 0) { throw "Database restore drill failed." }
    & docker @compose run --rm object-restore-drill
    if ($LASTEXITCODE -ne 0) { throw "Object restore drill failed." }
    & docker @compose run --rm restore-verify
    if ($LASTEXITCODE -ne 0) { throw "Tombstone replay or restored-system validation failed." }
}
finally {
    $env:RESTORE_ROOT = $previousRestoreRoot
    $env:RESTORE_DATABASE = $previousRestoreDatabase
    $env:RESTORE_PREFIX = $previousRestorePrefix
    if (Test-Path -LiteralPath $restoreRoot) {
        Remove-Item -LiteralPath (Assert-ChildPath $safeRoot $restoreRoot) -Recurse -Force
    }
    if (Test-Path -LiteralPath $archive) {
        Remove-Item -LiteralPath (Assert-ChildPath $safeRoot $archive) -Force
    }
    if (Test-Path -LiteralPath $tombstoneRoot) {
        Remove-Item -LiteralPath (Assert-ChildPath $safeRoot $tombstoneRoot) -Recurse -Force
    }
    if (Test-Path -LiteralPath $tombstoneArchive) {
        Remove-Item -LiteralPath (Assert-ChildPath $safeRoot $tombstoneArchive) -Force
    }
}

$elapsed = [DateTime]::UtcNow - $started
Write-Output "Restore drill completed into isolated targets; no production traffic was changed."
Write-Output ("Duration seconds: {0:N1}" -f $elapsed.TotalSeconds)
