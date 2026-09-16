# ApexTrader Task Scheduler launcher
# Called by Windows Task Scheduler - avoids quoting issues with inline -Command
$ErrorActionPreference = 'Continue'

$BaseDir = "C:\Users\user\Desktop\wsapex\apextrader"
$Python  = "$BaseDir\apextrader\Scripts\python.exe"
$Script  = "$BaseDir\autobot.py"
$Log     = "$BaseDir\autobot_scheduler.log"

Set-Location $BaseDir

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [TASK] ApexTraderAutoRun triggered by Task Scheduler" | Tee-Object -FilePath $Log -Append

& $Python $Script 2>&1 | Tee-Object -FilePath $Log -Append
