<#
.SYNOPSIS
    Installs the Stallion Tally agent on a Windows machine that runs TallyPrime.

.DESCRIPTION
    - Finds Python 3.12+ (or tells you how to install it)
    - Creates a virtual environment in .venv
    - Installs the application and its dependencies
    - Creates .env from .env.example if it does not exist
    - Initialises the local database and runs a connection test

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipConnectionTest
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root
Write-Host "== Stallion Tally agent setup ==" -ForegroundColor Cyan
Write-Host "Install directory: $Root"

function Get-PythonCommand {
    $candidates = @(
        @{ Exe = "py";      Args = @("-3.13") },
        @{ Exe = "py";      Args = @("-3.12") },
        @{ Exe = "python";  Args = @() },
        @{ Exe = "python3"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $version = & $c.Exe @($c.Args + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
            if (-not $version) { continue }
            $parts = $version.Trim().Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
                return @{ Exe = $c.Exe; Args = $c.Args; Version = $version.Trim() }
            }
        } catch { continue }
    }
    return $null
}

$python = Get-PythonCommand
if (-not $python) {
    Write-Host ""
    Write-Host "Python 3.12 or newer was not found." -ForegroundColor Red
    Write-Host "Install it with ONE of the following, then run this script again:"
    Write-Host "  1) winget install Python.Python.3.12"
    Write-Host "  2) Download from https://www.python.org/downloads/windows/"
    Write-Host "     (tick 'Add python.exe to PATH' in the installer)"
    exit 1
}
Write-Host "Using Python $($python.Version) ($($python.Exe) $($python.Args -join ' '))"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment .venv ..."
    & $python.Exe @($python.Args + @("-m", "venv", ".venv"))
}
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"

Write-Host "Installing the application and dependencies (this can take a few minutes) ..."
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install --quiet .
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "Created .env from .env.example." -ForegroundColor Yellow
    Write-Host "Edit .env and set at least:"
    Write-Host "  AZURE_STORAGE_BACKEND   (local to test without Azure, azure for real uploads)"
    Write-Host "  AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL"
}

New-Item -ItemType Directory -Force -Path "data", "data\raw", "data\exports", "data\logs" | Out-Null

$cli = Join-Path $Root ".venv\Scripts\stallion-tally.exe"
Write-Host ""
& $cli init
Write-Host ""
& $cli config
if (-not $SkipConnectionTest) {
    Write-Host ""
    & $cli test-connection
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Tally did not answer. Make sure TallyPrime is open, a company is loaded and" -ForegroundColor Yellow
        Write-Host "F1 Help > Settings > Connectivity > Client/Server configuration is set to" -ForegroundColor Yellow
        Write-Host "'TallyPrime acts as: Both' with port 9000 (or the value of TALLY_PORT in .env)." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Setup finished." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  .\scripts\run_sync.ps1                 # one full sync (Tally -> SQLite -> Azure/local)"
Write-Host "  .\.venv\Scripts\stallion-tally.exe status"
Write-Host "  .\scripts\install_service.ps1          # run automatically at startup (as Administrator)"
