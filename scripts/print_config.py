"""
CLI utility to print and validate ApexTrader config.
Run with: python scripts/print_config.py
"""
import os
import sys
from engine import config

def print_config():
    print("\nApexTrader Effective Configuration\n" + "-"*40)
    for k in dir(config):
        if k.isupper() and not k.startswith("_"):
            v = getattr(config, k)
            print(f"{k:30} = {v}")
    print("\nAll values reflect .env and environment overrides.")

def validate_config():
    errors = []
    # Example required vars (expand as needed)
    required = [
        ("API_KEY", config.API_KEY),
        ("API_SECRET", config.API_SECRET),
    ]
    for name, value in required:
        if not value:
            errors.append(f"Missing required config: {name}")
    if errors:
        print("\n[CONFIG VALIDATION ERRORS]")
        for e in errors:
            print("-", e)
        sys.exit(1)
    else:
        print("\nConfig validation passed.")

if __name__ == "__main__":
    print_config()
    validate_config()
