<#
.SYNOPSIS
    Writes the Excel verification workbook(s) for a Tally expert and opens the folder.
    Reads the local database only; run a sync first.

.EXAMPLE
    .\scripts\export_excel.ps1
    .\scripts\export_excel.ps1 --company "Stallion Automotive"
    .\scripts\export_excel.ps1 --from 2026-04-01 --to 2026-09-30
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExcelArgs
)

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root
$cli = Join-Path $Root ".venv\Scripts\stallion-tally.exe"
if (-not (Test-Path $cli)) {
    Write-Host "The agent is not installed. Run scripts\setup_windows.ps1 first." -ForegroundColor Red
    exit 3
}
& $cli excel @ExcelArgs
$code = $LASTEXITCODE
$folder = Join-Path $Root "data\exports\excel"
if ($code -eq 0 -and (Test-Path $folder) -and -not ($ExcelArgs -contains "--output" -or $ExcelArgs -contains "-o")) {
    Start-Process explorer.exe $folder
}
exit $code
