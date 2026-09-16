"""Deep squeeze timing analysis — already popped vs ready to pop."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from engine.utils import get_bars, calc_rsi
import pandas as pd

CANDIDATES = ["IBRX", "TMDX", "BFLY", "VSH", "DOCN", "BKSY", "TARS", "HIMS", "GRPN", "KRUS", "BBNX"]

def analyze(sym):
    try:
        bars = get_bars(sym, "90d", "1d")
        if bars.empty or len(bars) < 30:
            return None

        closes = bars["close"]
        highs  = bars["high"]
        lows   = bars["low"]
        vols   = bars["volume"]

        spot      = float(closes.iloc[-1])
        hi52w     = float(highs.max())
        lo52w     = float(lows.min())
        pct_from_high = (spot - hi52w) / hi52w * 100

        # RSI
        rsi_ser = calc_rsi(closes)
        rsi = float(rsi_ser.iloc[-1]) if rsi_ser is not None and not rsi_ser.empty else None

        # EMAs
        ema8  = float(closes.ewm(span=8,  adjust=False).mean().iloc[-1])
        ema21 = float(closes.ewm(span=21, adjust=False).mean().iloc[-1])
        ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])

        # Volume ratio (today vs 20d avg)
        avg_vol = float(vols.iloc[-21:-1].mean())
        today_vol = float(vols.iloc[-1])
        vol_ratio = today_vol / avg_vol if avg_vol > 0 else 0

        # 5-day return
        ret5d = (spot - float(closes.iloc[-6])) / float(closes.iloc[-6]) * 100 if len(closes) >= 6 else 0

        # 20-day return
        ret20d = (spot - float(closes.iloc[-21])) / float(closes.iloc[-21]) * 100 if len(closes) >= 21 else 0

        # 3-day EMA trend (is ema8 rising?)
        ema8_3d_ago = float(closes.iloc[-4:].ewm(span=8, adjust=False).mean().iloc[0])
        ema8_rising = ema8 > ema8_3d_ago

        # Consecutive up/down days
        changes = closes.diff().iloc[-6:]
        up_streak = 0
        for ch in reversed(changes.tolist()):
            if ch > 0:
                up_streak += 1
            else:
                break
        dn_streak = 0
        for ch in reversed(changes.tolist()):
            if ch < 0:
                dn_streak += 1
            else:
                break

        # Verdict
        already_popped = (
            rsi is not None and rsi > 72 and
            pct_from_high > -8 and
            ret20d > 40
        )
        coiling = (
            rsi is not None and 40 <= rsi <= 65 and
            ema8 > ema21 and
            ema8_rising and
            pct_from_high < -15 and
            vol_ratio < 1.5
        )
        setting_up = (
            rsi is not None and 35 <= rsi <= 70 and
            ema8 > ema21 and
            not already_popped
        )

        if already_popped:
            verdict = "POPPED - chasing"
        elif coiling:
            verdict = "COILING - ideal entry"
        elif setting_up:
            verdict = "SETTING UP"
        else:
            verdict = "NO SETUP"

        return dict(
            sym=sym, spot=spot, rsi=rsi,
            ema8=ema8, ema21=ema21, ema50=ema50,
            ema_stack=(ema8 > ema21 > ema50),
            ema8_rising=ema8_rising,
            vol_ratio=vol_ratio,
            ret5d=ret5d, ret20d=ret20d,
            pct_from_high=pct_from_high,
            hi52w=hi52w,
            up_streak=up_streak, dn_streak=dn_streak,
            verdict=verdict,
        )
    except Exception as e:
        return dict(sym=sym, verdict=f"ERROR: {e}")

results = []
for sym in CANDIDATES:
    r = analyze(sym)
    if r:
        results.append(r)

# Sort: COILING first, then SETTING UP, then rest
order = {"COILING - ideal entry": 0, "SETTING UP": 1, "POPPED - chasing": 2, "NO SETUP": 3}
results.sort(key=lambda x: order.get(x.get("verdict","NO SETUP"), 9))

print()
print(f"{'Sym':<6} {'Price':>7} {'RSI':>5} {'EMA8>21>50':>11} {'EMA8^':>6} {'VolR':>5} {'5d%':>6} {'20d%':>6} {'Off52H':>7}  Verdict")
print("-" * 105)
for r in results:
    if "ERROR" in r.get("verdict",""):
        print(f"{r['sym']:<6}  {r['verdict']}")
        continue
    stack = "YES" if r["ema_stack"] else ("8>21" if r["ema8"] > r["ema21"] else "NO")
    rising = "^" if r["ema8_rising"] else "v"
    streaks = f"up{r['up_streak']}" if r["up_streak"] >= 2 else (f"dn{r['dn_streak']}" if r["dn_streak"] >= 2 else "  -")
    print(f"{r['sym']:<6} {r['spot']:>7.2f} {r['rsi']:>5.1f} {stack:>11} {rising:>6} {r['vol_ratio']:>5.2f} {r['ret5d']:>+6.1f}% {r['ret20d']:>+6.1f}% {r['pct_from_high']:>+7.1f}%  {r['verdict']}  {streaks}")
