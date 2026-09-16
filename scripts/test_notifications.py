"""ApexTrader A+ Options scan email test — sends a formatted test with sample picks.
Run with:
  python scripts/test_notifications.py
Requirements:
  - .env SMTP vars configured
  - USE_EMAIL_NOTIFICATIONS=true (auto-forced in this script)
"""

import os, sys, datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

os.environ["USE_EMAIL_NOTIFICATIONS"] = "true"   # force on for test

from dataclasses import dataclass
from engine.notifications.notifications import build_top5_report, send_email

@dataclass
class _S:
    symbol:     str
    action:     str
    price:      float
    confidence: float
    strategy:   str
    reason:     str

sample_picks = [
    _S("APA",  "buy", 41.35, 0.95, "Options/PUT",
       "$42P 2026-04-10 9DTE mid=$1.48 BEven=$40.52 IVrank=32 R/R=2.1x chg=-5.2% vol=3.2x"),
    _S("AVNW", "buy", 19.67, 0.95, "Options/PUT",
       "$20P 2026-04-10 9DTE mid=$0.85 BEven=$19.15 IVrank=28 R/R=1.8x chg=-4.8% vol=7.6x"),
    _S("ERX",  "buy", 95.40, 0.92, "Options/PUT",
       "$96P 2026-04-10 9DTE mid=$2.10 BEven=$93.90 IVrank=44 R/R=1.7x chg=-7.4% vol=2.1x"),
]

report = build_top5_report(sample_picks, datetime.date.today(), sentiment="bearish", regime="bear")

print(f"Subject : {report['subject']}")
print(f"\n--- Plain text ---\n{report['text']}")
print(f"\n--- Sending email to {os.getenv('EMAIL_TO_ADDRESSES', '(not set)')} ---")

result = send_email(report["subject"], report["text"], report["html"])
print("Result  :", "SENT OK" if result else "SKIPPED (email disabled or SMTP not configured)")

