#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-paper}"
if [[ "$MODE" != "paper" && "$MODE" != "live" ]]; then
  echo "Usage: ./run_local_sh.sh [paper|live]"
  exit 1
fi

if [ -f "apextrader/bin/activate" ]; then
  source apextrader/bin/activate
  echo "Virtualenv activated"
else
  echo "Warning: apextrader/bin/activate not found"
fi

export TRADE_MODE="$MODE"

if [[ "$MODE" == "paper" ]]; then
  [[ -z "${PAPER_ALPACA_API_KEY:-}" ]] && echo "Warning: PAPER_ALPACA_API_KEY not set in .env"
else
  [[ -z "${LIVE_ALPACA_API_KEY:-}" ]] && echo "Warning: LIVE_ALPACA_API_KEY not set in .env"
fi

echo "Running main.py in $MODE mode (TRADE_MODE=$TRADE_MODE)"
python main.py
