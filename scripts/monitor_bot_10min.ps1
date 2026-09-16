$ErrorActionPreference = 'SilentlyContinue'

$durationSec = 600
$pollSec     = 15
$restartCount = 0
$start = Get-Date
$end   = $start.AddSeconds($durationSec)
$root  = (Get-Location).Path
$py      = Join-Path $root 'apextrader\Scripts\python.exe'
$runner  = Join-Path $root 'autobot.py'
$pidFile = Join-Path $root 'autobot.pid'
$envFile = Join-Path $root '.env'
$logCandidates = @(
    (Join-Path $root 'autobot.log'),
    (Join-Path $root 'apextrader.log')
)

# ── Load .env so we can resolve mode-specific keys ──────────────────────────
$envVars = @{}
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' } | ForEach-Object {
        $k, $v = $_ -split '=', 2
        $envVars[$k.Trim()] = $v.Trim()
    }
}

function Get-ActiveMode {
    # Check autobot.log for most recent mode line
    $al = Join-Path $root 'autobot.log'
    if (Test-Path $al) {
        $modeLine = Get-Content $al | Select-String 'mode=LIVE|mode=PAPER' | Select-Object -Last 1
        if ($modeLine) {
            if ($modeLine -match 'mode=LIVE')  { return 'LIVE'  }
            if ($modeLine -match 'mode=PAPER') { return 'PAPER' }
        }
    }
    # Fallback: read TRADE_MODE from .env
    $tradeMode = $envVars['TRADE_MODE']
    return if ($tradeMode -eq 'live') { 'LIVE' } else { 'PAPER' }
}

function Get-ActiveKeyPrefix {
    $mode = Get-ActiveMode
    $keyVar = if ($mode -eq 'LIVE') { 'LIVE_ALPACA_API_KEY' } else { 'PAPER_ALPACA_API_KEY' }
    $key = $envVars[$keyVar]
    if ($key -and $key.Length -ge 6) { return "$($key.Substring(0,6))***" }
    return '(not set)'
}

$seen = @{}
foreach ($lp in $logCandidates) {
    $seen[$lp] = if (Test-Path $lp) { (Get-Content $lp | Measure-Object -Line).Lines } else { 0 }
}

function Get-WatchdogAlive {
    if (-not (Test-Path $pidFile)) { return $false }
    $pidTxt = (Get-Content $pidFile | Select-Object -First 1).Trim()
    if ($pidTxt -notmatch '^\d+$') { return $false }
    return $null -ne (Get-Process -Id ([int]$pidTxt) -ErrorAction SilentlyContinue)
}

function Start-BotWatchdog {
    if (-not (Test-Path $py) -or -not (Test-Path $runner)) { return $false }
    # Inject correct key set for current mode before spawning
    $mode = Get-ActiveMode
    $keyVar = if ($mode -eq 'LIVE') { 'LIVE_ALPACA_API_KEY' } else { 'PAPER_ALPACA_API_KEY' }
    $env:TRADE_MODE = $mode.ToLower()
    Start-Process -FilePath $py -ArgumentList $runner -WorkingDirectory $root -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 4
    return $true
}

# ── Banner ───────────────────────────────────────────────────────────────────
$initMode = Get-ActiveMode
$initKey  = Get-ActiveKeyPrefix
Write-Output "============================================================"
Write-Output ("MONITOR_START  {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Output ("MODE={0}  KEY={1}  POLL={2}s  DURATION={3}s" -f $initMode, $initKey, $pollSec, $durationSec)
Write-Output "============================================================"

# ── Main loop ────────────────────────────────────────────────────────────────
while ((Get-Date) -lt $end) {
    $wdAlive  = Get-WatchdogAlive
    $mode     = Get-ActiveMode
    $keyPfx   = Get-ActiveKeyPrefix
    $elapsed  = [int]((Get-Date) - $start).TotalSeconds
    $remaining = $durationSec - $elapsed

    if (-not $wdAlive) {
        Write-Output ("  [{0}] WATCHDOG_DOWN - restarting (mode={1} key={2})" -f (Get-Date -Format 'HH:mm:ss'), $mode, $keyPfx)
        $null = Start-BotWatchdog
        $restartCount++
        if (Get-WatchdogAlive) {
            Write-Output ("  [{0}] HEAL_OK restart#{1}" -f (Get-Date -Format 'HH:mm:ss'), $restartCount)
        } else {
            Write-Output ("  [{0}] HEAL_FAILED - check runner/keys" -f (Get-Date -Format 'HH:mm:ss'))
        }
    } else {
        Write-Output ("  [{0}] OK  mode={1}  key={2}  remaining={3}s" -f (Get-Date -Format 'HH:mm:ss'), $mode, $keyPfx, $remaining)
    }

    # ── Scan new log lines for errors ────────────────────────────────────────
    foreach ($lp in $logCandidates) {
        if (-not (Test-Path $lp)) { continue }
        $all       = Get-Content $lp
        $lineCount = ($all | Measure-Object -Line).Lines
        $prev      = $seen[$lp]
        if ($lineCount -gt $prev) {
            $new = $all[$prev..($lineCount - 1)]
            $errors = $new | Select-String -Pattern '\[ERROR\]|\[CRITICAL\]|Traceback|Exception'
            if ($errors) {
                Write-Output ("  [{0}] ERROR in {1}:" -f (Get-Date -Format 'HH:mm:ss'), ([System.IO.Path]::GetFileName($lp)))
                $errors | ForEach-Object { Write-Output ("    >> {0}" -f $_.Line.Trim()) }
            }
            $seen[$lp] = $lineCount
        }
    }

    Start-Sleep -Seconds $pollSec
}

# ── Final summary ────────────────────────────────────────────────────────────
$finalMode = Get-ActiveMode
$finalKey  = Get-ActiveKeyPrefix
Write-Output "============================================================"
Write-Output ("MONITOR_END  {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Output ("MODE={0}  KEY={1}  RESTARTS={2}  WATCHDOG={3}" -f $finalMode, $finalKey, $restartCount, (Get-WatchdogAlive))
