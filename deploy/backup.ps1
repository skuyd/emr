[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,
    [Parameter(Mandatory = $true)]
    [string]$AgeRecipient,
    [int]$RetentionDays = 30
)

$ErrorActionPreference = "Stop"
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$environmentFile = Join-Path $repositoryRoot ".env.production"
$composeFile = Join-Path $PSScriptRoot "compose.yaml"

function Resolve-SafeRoot([string]$PathValue) {
    $absolute = [IO.Path]::GetFullPath($PathValue)
    $volumeRoot = [IO.Path]::GetPathRoot($absolute)
    if ($absolute.TrimEnd('\', '/') -eq $volumeRoot.TrimEnd('\', '/')) {
        throw "BackupRoot must not be a filesystem root."
    }
    New-Item -ItemType Directory -Path $absolute -Force | Out-Null
    return (Get-Item -LiteralPath $absolute).FullName
}

function Assert-ChildPath([string]$Root, [string]$Candidate) {
    $rootPrefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $absoluteCandidate = [IO.Path]::GetFullPath($Candidate)
    if (-not $absoluteCandidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside BackupRoot."
    }
    return $absoluteCandidate
}

foreach ($command in ("docker", "age", "tar")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command is unavailable: $command"
    }
}
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw ".env.production does not exist."
}
if ($RetentionDays -lt 1 -or $RetentionDays -gt 30) {
    throw "RetentionDays must be between 1 and 30."
}

$safeRoot = Resolve-SafeRoot $BackupRoot
$backupName = "phr-backup-" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$snapshot = Assert-ChildPath $safeRoot (Join-Path $safeRoot $backupName)
$archive = Assert-ChildPath $safeRoot (Join-Path $safeRoot ($backupName + ".tar.gz"))
$encrypted = Assert-ChildPath $safeRoot ($archive + ".age")
foreach ($target in ($snapshot, $archive, $encrypted)) {
    if (Test-Path -LiteralPath $target) {
        throw "Refusing to overwrite an existing backup artifact."
    }
}
New-Item -ItemType Directory -Path $snapshot | Out-Null

$started = [DateTime]::UtcNow
$backupCompleted = $false
$previousBackupRoot = $env:BACKUP_ROOT
$previousBackupName = $env:BACKUP_NAME
try {
    $env:BACKUP_ROOT = $safeRoot
    $env:BACKUP_NAME = $backupName
    $compose = @("compose", "--env-file", $environmentFile, "--file", $composeFile, "--profile", "tools")
    & docker @compose run --rm db-backup
    if ($LASTEXITCODE -ne 0) { throw "Database backup failed." }
    & docker @compose run --rm object-backup
    if ($LASTEXITCODE -ne 0) { throw "Object backup failed." }
    & docker @compose run --rm tombstone-backup
    if ($LASTEXITCODE -ne 0) { throw "Tombstone backup failed." }

    if (Get-ChildItem -LiteralPath $snapshot -Recurse -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue) {
        throw "Backup snapshot must not contain links or reparse points."
    }

    $metadataPath = Join-Path $snapshot "backup-metadata.json"
    $metadata = [ordered]@{
        schema_version = 1
        backup_name = $backupName
        created_at_utc = [DateTime]::UtcNow.ToString("o")
        includes = @("database", "objects", "deletion-tombstones")
    } | ConvertTo-Json -Depth 3
    [IO.File]::WriteAllText($metadataPath, $metadata + "`n", [Text.UTF8Encoding]::new($false))

    $manifestPath = Join-Path $snapshot "manifest.sha256"
    $manifestLines = Get-ChildItem -LiteralPath $snapshot -Recurse -File |
        Where-Object { $_.FullName -ne $manifestPath } |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($snapshot.Length + 1).Replace('\', '/')
            $digest = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            "$digest  $relative"
        }
    [IO.File]::WriteAllLines($manifestPath, $manifestLines, [Text.UTF8Encoding]::new($false))

    & tar -C $safeRoot -czf $archive $backupName
    if ($LASTEXITCODE -ne 0) { throw "Backup archive creation failed." }
    & age --recipient $AgeRecipient --output $encrypted $archive
    if ($LASTEXITCODE -ne 0) { throw "Backup encryption failed." }
    $backupCompleted = $true
}
finally {
    $env:BACKUP_ROOT = $previousBackupRoot
    $env:BACKUP_NAME = $previousBackupName
    if (Test-Path -LiteralPath $snapshot) {
        Remove-Item -LiteralPath (Assert-ChildPath $safeRoot $snapshot) -Recurse -Force
    }
    if (Test-Path -LiteralPath $archive) {
        Remove-Item -LiteralPath (Assert-ChildPath $safeRoot $archive) -Force
    }
    if (-not $backupCompleted -and (Test-Path -LiteralPath $encrypted)) {
        Remove-Item -LiteralPath (Assert-ChildPath $safeRoot $encrypted) -Force
    }
}

$cutoff = [DateTime]::UtcNow.AddDays(-$RetentionDays)
Get-ChildItem -LiteralPath $safeRoot -File -Filter "phr-backup-*.tar.gz.age" |
    Where-Object { $_.LastWriteTimeUtc -lt $cutoff -and $_.FullName -ne $encrypted } |
    ForEach-Object { Remove-Item -LiteralPath (Assert-ChildPath $safeRoot $_.FullName) -Force }

$elapsed = [DateTime]::UtcNow - $started
Write-Output ("Encrypted backup created: {0}" -f $encrypted)
Write-Output ("Duration seconds: {0:N1}" -f $elapsed.TotalSeconds)
