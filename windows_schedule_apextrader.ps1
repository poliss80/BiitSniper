# Windows Scheduled Task Install for ApexTrader
# Run in PowerShell as Administrator.

$botDir  = 'C:\Users\spoli\Desktop\BiStrider_TS\BitStrider'
$python  = "$botDir\apextrader\Scripts\python.exe"
$script  = "$botDir\autobot.py"

# ── Task 1: Start bot at 7:00 AM ET Mon-Fri ─────────────────────────────────
$startAction = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -Command `"Set-Location '$botDir'; `$env:TRADE_MODE='live'; & '$python' '$script'`"" `
    -WorkingDirectory $botDir

$startTrigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 07:00
$principal     = New-ScheduledTaskPrincipal -UserId "$env:UserName" -LogonType Interactive -RunLevel Highest
$settings      = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -Hidden

Register-ScheduledTask -TaskName 'ApexTrader_Start' -Action $startAction -Trigger $startTrigger `
    -Principal $principal -Settings $settings -Description 'Start ApexTrader at 7:00 AM ET on weekdays' -Force

# ── Task 2: Stop bot at 8:30 PM ET Mon-Fri ───────────────────────────────────
$stopAction = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -Command `"taskkill /F /IM python.exe; taskkill /F /IM msedge.exe; Remove-Item '$botDir\autobot.pid' -ErrorAction SilentlyContinue`""

$stopTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 20:30

Register-ScheduledTask -TaskName 'ApexTrader_Stop' -Action $stopAction -Trigger $stopTrigger `
    -Principal $principal -Settings $settings -Description 'Stop ApexTrader at 8:30 PM ET on weekdays' -Force

Write-Host "Done. Two tasks registered:"
Write-Host "  ApexTrader_Start — 7:00 AM Mon-Fri"
Write-Host "  ApexTrader_Stop  — 8:30 PM Mon-Fri"
Write-Host "Run: schtasks /query /tn ApexTrader_Start to verify."