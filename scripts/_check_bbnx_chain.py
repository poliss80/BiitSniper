"""Check BBNX chain detail."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
import logging
logging.basicConfig(level=logging.DEBUG, format="%(message)s")
import datetime
from engine.options.strategies import _get_options_chain, _pick_strike, _fetch_bar_context

sym   = "BBNX"
ctx   = _fetch_bar_context(sym)
chain = _get_options_chain(sym)
dte   = (chain.expiry - datetime.date.today()).days
print(f"spot={ctx.spot:.2f}  iv_rank={chain.iv_rank:.1f}  expiry={chain.expiry}  dte={dte}")
print("calls available:")
print(chain.calls[["strike","bid","ask","delta","openinterest"]].to_string())
lr = _pick_strike(chain.calls, ctx.spot, 0.32)
if lr is not None:
    ls  = float(lr["strike"])
    lbid = float(lr.get("bid", 0)) if "bid" in lr.index else 0
    lask = float(lr.get("ask", 0)) if "ask" in lr.index else 0
    lmid = (lbid + lask) / 2.0
    print(f"\nlong_row: strike={ls} bid={lbid} ask={lask} mid={lmid}")
    print(f"  net_debit limit: {ctx.spot * 0.05:.2f} (5% of spot)")
    strikes = sorted(chain.calls["strike"].unique())
    li = next((i for i,s in enumerate(strikes) if abs(s-ls)<0.01), None)
    if li is not None:
        si = min(li+2, len(strikes)-1)
        ss = strikes[si]
        sr = chain.calls[abs(chain.calls["strike"]-ss)<0.01].iloc[0]
        sbid = float(sr.get("bid",0)) if "bid" in sr.index else 0
        sask = float(sr.get("ask",0)) if "ask" in sr.index else 0
        smid = (sbid + sask) / 2.0
        net  = round(lmid - smid, 3)
        width = ss - ls
        print(f"short_row: strike={ss} bid={sbid} ask={sask} mid={smid}")
        print(f"net_debit={net} spread_width={width} debit/width={net/width if width>0 else 'N/A':.2f} rr={round((width-net)/net,2) if net>0 else 'N/A'}")
else:
    print("_pick_strike returned None")
