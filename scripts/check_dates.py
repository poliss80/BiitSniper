#!/usr/bin/env python
"""Check system date and DTE window."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datetime import date, timedelta
from engine.config import OPTIONS_DTE_MIN, OPTIONS_DTE_MAX

today = date.today()
exp_gte = today + timedelta(days=OPTIONS_DTE_MIN)
exp_lte = today + timedelta(days=OPTIONS_DTE_MAX)

print(f"Today: {today}")
print(f"OPTIONS_DTE_MIN: {OPTIONS_DTE_MIN}")
print(f"OPTIONS_DTE_MAX: {OPTIONS_DTE_MAX}")
print(f"DTE Window: {exp_gte} to {exp_lte}")
print(f"Available expirations: 2026-05-06 (1 DTE), 2026-05-08 (3 DTE), etc.")
print(f"Closest match in window: Would need to look for >= {exp_gte}")
