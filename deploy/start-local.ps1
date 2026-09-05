[CmdletBinding()]
param(
    [switch]$NoWorker,
    [switch]$NoSeed,
    [ValidateNotNullOrEmpty()]
    [string]$Address = "127.0.0.1:8000",
    [ValidateNotNullOrEmpty()]
    [string]$PythonPath = "D:\EMR-Runtime\PaddleOCR\.venv\Scripts\python.exe"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Address -notmatch "^(127\.0\.0\.1|localhost):([0-9]{1,5})$") {
    throw "Address must be a loopback address in the form 127.0.0.1:PORT or localhost:PORT."
}
$port = [int]$Matches[2]
if ($port -lt 1 -or $port -gt 65535) {
    throw "Address port must be between 1 and 65535."
}

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$managePy = Join-Path $repositoryRoot "manage.py"
$environmentFile = Join-Path $repositoryRoot ".env"
if (-not (Test-Path -LiteralPath $managePy -PathType Leaf)) {
    throw "manage.py was not found at $managePy."
}
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw ".env is missing. Run 'python deploy/bootstrap_dev_env.py' first."
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python executable was not found at $PythonPath. Use -PythonPath to select another environment."
}
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path

function Invoke-ManagementCommand {
    param([Parameter(Mandatory = $true)][string[]]$CommandArguments)

    & $resolvedPython $managePy @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Django management command failed: $($CommandArguments -join ' ')"
    }
}

$previousSettingsModule = [Environment]::GetEnvironmentVariable("DJANGO_SETTINGS_MODULE", "Process")
$workerProcess = $null

try {
    $env:DJANGO_SETTINGS_MODULE = "config.settings.dev"
    Push-Location $repositoryRoot
    try {
        Write-Output "Applying local database migrations..."
        Invoke-ManagementCommand -CommandArguments @("migrate", "--noinput")

        Write-Output "Checking local Django configuration..."
        Invoke-ManagementCommand -CommandArguments @("check")

        if (-not $NoSeed) {
            Write-Output "Creating or refreshing the local development account..."
            Invoke-ManagementCommand -CommandArguments @("seed_development_account")
        }

        if (-not $NoWorker) {
            $logRoot = Join-Path $repositoryRoot ".runtime\logs"
            New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
            $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $workerStdout = Join-Path $logRoot "local-processing-worker-$timestamp.stdout.log"
            $workerStderr = Join-Path $logRoot "local-processing-worker-$timestamp.stderr.log"
            $workerProcess = Start-Process `
                -FilePath $resolvedPython `
                -ArgumentList ('"{0}" run_local_processing_worker' -f $managePy) `
                -WorkingDirectory $repositoryRoot `
                -WindowStyle Hidden `
                -RedirectStandardOutput $workerStdout `
                -RedirectStandardError $workerStderr `
                -PassThru
            Start-Sleep -Milliseconds 500
            $workerProcess.Refresh()
            if ($workerProcess.HasExited) {
                throw "Local processing worker exited during startup. Review $workerStderr."
            }
            Write-Output "Local processing worker started (PID $($workerProcess.Id))."
            Write-Output "Worker logs: $workerStdout and $workerStderr"
        }

        Write-Output "Starting the local application at http://$Address"
        Write-Output "Press Ctrl+C to stop the web server and local worker."
        & $resolvedPython $managePy runserver $Address
        if ($LASTEXITCODE -ne 0) {
            throw "Django development server exited with code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }
} finally {
    if ($null -ne $workerProcess) {
        $workerProcess.Refresh()
        if (-not $workerProcess.HasExited) {
            Stop-Process -Id $workerProcess.Id
            Wait-Process -Id $workerProcess.Id -ErrorAction SilentlyContinue
        }
        Write-Output "Local processing worker stopped."
    }
    if ($null -eq $previousSettingsModule) {
        Remove-Item -LiteralPath "Env:DJANGO_SETTINGS_MODULE" -ErrorAction SilentlyContinue
    } else {
        $env:DJANGO_SETTINGS_MODULE = $previousSettingsModule
    }
}
