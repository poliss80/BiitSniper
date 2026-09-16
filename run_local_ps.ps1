# ApexTrader local runner (Windows PowerShell)
param(
    [ValidateSet('paper', 'live')]
    [string]$Mode = 'paper'
)

$ErrorActionPreference = 'Stop'

$venvActivate = Join-Path $PSScriptRoot 'apextrader\Scripts\Activate.ps1'
if (Test-Path $venvActivate) {
    Write-Host "Activating venv: $venvActivate"
    . $venvActivate
} else {
    Write-Warning "Virtualenv activation script not found at $venvActivate"
}

$env:TRADE_MODE = $Mode

$modeKey = if ($Mode -eq 'paper') { $env:PAPER_ALPACA_API_KEY } else { $env:LIVE_ALPACA_API_KEY }
if (-not $modeKey) {
    Write-Warning "Keys not set: define PAPER_ALPACA_API_KEY or LIVE_ALPACA_API_KEY in .env"
}

Write-Host "Launching main.py in $Mode mode (TRADE_MODE=$($env:TRADE_MODE))"
python .\main.py
