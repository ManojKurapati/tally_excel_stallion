<#
.SYNOPSIS
    Registers the Stallion Tally agent so it starts automatically with Windows.

.DESCRIPTION
    Two modes:

      Task    (default)  A Windows Scheduled Task that starts at boot, runs as SYSTEM,
                         and is restarted automatically if it stops. Needs nothing extra.

      Service            A real Windows service using NSSM (https://nssm.cc). Place
                         nssm.exe in <install dir>\tools\ or pass -NssmPath.

    In both modes the agent runs `stallion-tally run`, which waits for TallyPrime,
    syncs every SYNC_INTERVAL_SECONDS and backs off while Tally is closed.
    Run this script from an elevated (Administrator) PowerShell.

.EXAMPLE
    .\scripts\install_service.ps1
    .\scripts\install_service.ps1 -Mode Service -NssmPath C:\tools\nssm.exe
    .\scripts\install_service.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [ValidateSet("Task", "Service")]
    [string]$Mode = "Task",
    [string]$Name = "StallionTallyAgent",
    [string]$NssmPath,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Cli = Join-Path $Root ".venv\Scripts\stallion-tally.exe"
$LogDir = Join-Path $Root "data\logs"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Please run this script from an Administrator PowerShell." -ForegroundColor Red
    exit 1
}
if (-not $Uninstall -and -not (Test-Path $Cli)) {
    Write-Host "The agent is not installed. Run scripts\setup_windows.ps1 first." -ForegroundColor Red
    exit 3
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Find-Nssm {
    if ($NssmPath -and (Test-Path $NssmPath)) { return (Resolve-Path $NssmPath).Path }
    $local = Join-Path $Root "tools\nssm.exe"
    if (Test-Path $local) { return $local }
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

if ($Mode -eq "Task") {
    if ($Uninstall) {
        if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $Name -Confirm:$false
            Write-Host "Scheduled task '$Name' removed."
        } else {
            Write-Host "Scheduled task '$Name' does not exist."
        }
        exit 0
    }
    $action = New-ScheduledTaskAction -Execute $Cli -Argument "run" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force `
        -Description "Stallion Tally agent: syncs TallyPrime data to Azure. Working directory $Root" | Out-Null
    Start-ScheduledTask -TaskName $Name
    Write-Host "Scheduled task '$Name' installed and started." -ForegroundColor Green
    Write-Host "It starts automatically at boot and restarts if it stops."
    Write-Host "Check progress with:  $Cli status"
    Write-Host "Logs: $LogDir\stallion_tally.log"
    exit 0
}

# --- NSSM service -------------------------------------------------------------
$nssm = Find-Nssm
if (-not $nssm) {
    Write-Host "nssm.exe was not found. Download it from https://nssm.cc/download, copy nssm.exe (win64) to" -ForegroundColor Red
    Write-Host "$Root\tools\nssm.exe or pass -NssmPath, or use the default -Mode Task instead." -ForegroundColor Red
    exit 1
}
if ($Uninstall) {
    & $nssm stop $Name 2>$null | Out-Null
    & $nssm remove $Name confirm
    Write-Host "Service '$Name' removed."
    exit 0
}
& $nssm install $Name $Cli run
& $nssm set $Name AppDirectory $Root
& $nssm set $Name DisplayName "Stallion Tally Agent"
& $nssm set $Name Description "Syncs TallyPrime data to Azure Blob Storage."
& $nssm set $Name Start SERVICE_AUTO_START
& $nssm set $Name AppStdout (Join-Path $LogDir "service-stdout.log")
& $nssm set $Name AppStderr (Join-Path $LogDir "service-stderr.log")
& $nssm set $Name AppRotateFiles 1
& $nssm set $Name AppRotateBytes 10485760
& $nssm set $Name AppExit Default Restart
& $nssm set $Name AppRestartDelay 60000
& $nssm set $Name AppStopMethodConsole 15000
& $nssm start $Name
Write-Host "Service '$Name' installed and started." -ForegroundColor Green
Write-Host "Manage it with: nssm status $Name | nssm restart $Name | services.msc"
