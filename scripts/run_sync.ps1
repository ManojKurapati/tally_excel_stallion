<#
.SYNOPSIS
    Runs one synchronisation cycle: TallyPrime -> SQLite -> Azure.

.EXAMPLE
    .\scripts\run_sync.ps1
    .\scripts\run_sync.ps1 --dataset ledgers
    .\scripts\run_sync.ps1 --from 2026-01-01 --to 2026-09-28
    .\scripts\run_sync.ps1 --no-export
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SyncArgs
)

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root
$cli = Join-Path $Root ".venv\Scripts\stallion-tally.exe"
if (-not (Test-Path $cli)) {
    Write-Host "The agent is not installed. Run scripts\setup_windows.ps1 first." -ForegroundColor Red
    exit 3
}
& $cli sync @SyncArgs
exit $LASTEXITCODE
