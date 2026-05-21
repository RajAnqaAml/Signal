# One-time setup: registers a Windows Scheduled Task that runs run_recorder.bat
# every Mon-Fri at 9:10 AM IST (5 min before market opens).
#
# Usage:
#   1. Right-click PowerShell, "Run as Administrator"  (not strictly required for
#      a user-scope task, but recommended to avoid prompts)
#   2. cd to this project folder
#   3. .\setup_scheduler.ps1
#
# To uninstall later:
#   Unregister-ScheduledTask -TaskName "NSE Signal Recorder" -Confirm:$false

$ErrorActionPreference = "Stop"

$TaskName    = "NSE Signal Recorder"
$ProjectDir  = $PSScriptRoot
$BatFile     = Join-Path $ProjectDir "run_recorder.bat"

if (-not (Test-Path $BatFile)) {
    Write-Error "run_recorder.bat not found at $BatFile"
    exit 1
}

Write-Host "Project dir : $ProjectDir"
Write-Host "Wrapper bat : $BatFile"
Write-Host "Task name   : $TaskName"
Write-Host ""

# Remove existing task if present (idempotent setup)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Action: run the bat file from the project dir
$Action = New-ScheduledTaskAction `
    -Execute $BatFile `
    -WorkingDirectory $ProjectDir

# Trigger: weekly Mon-Fri at 9:10 AM local time
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "9:10AM"

# Settings: allow on battery, start if missed (e.g. PC was off at 9:10)
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 7)

# Run as current user, interactively (only when logged in — no password needed)
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Records NSE option signals every 10 min during market hours (9:15-15:30 IST). Exits at 15:35."

Write-Host ""
Write-Host "Task registered. Verify with:" -ForegroundColor Green
Write-Host "  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "Manual test run:" -ForegroundColor Green
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "Disable when not testing:" -ForegroundColor Yellow
Write-Host "  Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "Logs will be written to: $ProjectDir\logs\recorder.log"
