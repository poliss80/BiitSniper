import re
from datetime import date

logs = ['autobot.log', f'apextrader.log.{date.today().isoformat()}', 'apextrader.log']
keywords = [
    'Launching main.py', 'Starting Orchestrator', 'Mode:', 'PAPER', 'LIVE', 'ERROR', 'Traceback',
    'No signals', 'No eligible', 'TOP5_', 'Skip ', 'BUY', 'SHORT', 'TP ', 'TIME LOSS', 'EOD CLOSE',
    'order error', 'order failed', 'insufficient', 'market closed', 'FORCE_SCAN'
]

# Create case-sensitive/literal match
print("--- LOG MATCHES ---")
for log_name in logs:
    print(f"=== {log_name} ===")
    try:
        with open(log_name, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        last_400 = lines[-400:] if len(lines) >= 400 else lines

        for line in last_400:
            if any(k in line for k in keywords):
                # Simple credential protection: redact anything that looks like schwab token/key or long hex/alphanumeric strings
                # especially in lines containing "token", "key", "secret", "password", "auth"
                line_redacted = line.strip()
                if any(sec in line_redacted.lower() for sec in ["token", "key", "secret", "password", "auth", "credential"]):
                    # Redact a 12+ char API/OAuth key or token pattern
                    line_redacted = re.sub(r'([A-Za-z0-9_\-\.\=\+\/]{12,})', '[REDACTED]', line_redacted)
                print(line_redacted)
    except Exception as e:
        print(f"Error reading {log_name}: {e}")

print("\n--- WATCHDOG MODE ---")
watchdog_mode = "UNKNOWN"
# Look for the last launching or environment configurations
for log_name in logs:
    try:
        with open(log_name, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        for line in reversed(lines):
            # Check for latest launch state or environment variable in logs
            if "Launching main.py" in line:
                if "PAPER" in line or "paper" in line:
                    watchdog_mode = f"PAPER (from: {line.strip()})"
                    break
                elif "LIVE" in line or "live" in line:
                    watchdog_mode = f"LIVE (from: {line.strip()})"
                    break
            elif "Starting Orchestrator" in line:
                # search for mode in recent surrounding lines
                if "mode=PAPER" in line or "mode=paper" in line:
                    watchdog_mode = f"PAPER (from: {line.strip()})"
                    break
                elif "mode=LIVE" in line or "mode=live" in line:
                    watchdog_mode = f"LIVE (from: {line.strip()})"
                    break
        if watchdog_mode != "UNKNOWN":
            break
    except Exception:
        pass

# Also check env variable from recent runs if NOT in logs
if watchdog_mode == "UNKNOWN":
    # fallback to searching simply Mode:
    for log_name in logs:
        try:
            with open(log_name, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            for line in reversed(lines):
                if "Mode:" in line or "TRADE_MODE" in line:
                    if "PAPER" in line or "paper" in line:
                        watchdog_mode = f"PAPER (from: {line.strip()})"
                        break
                    elif "LIVE" in line or "live" in line:
                        watchdog_mode = f"LIVE (from: {line.strip()})"
                        break
            if watchdog_mode != "UNKNOWN":
                break
        except Exception:
            pass

print(f"Latest watchdog/orchestrator launch mode: {watchdog_mode}")

print("\n--- LAST 5 ERRORS/WARNINGS ---")
errs_warnings = []
for log_name in logs:
    try:
        with open(log_name, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        # Search all lines of logs for Errors or Warnings
        for idx, line in enumerate(lines):
            line_upper = line.upper()
            if "ERROR" in line_upper or "WARNING" in line_upper or "TRACEBACK" in line_upper or "EXCEPTION" in line_upper or "FAILED" in line_upper:
                # Redact
                line_redacted = line.strip()
                if any(sec in line_redacted.lower() for sec in ["token", "key", "secret", "password", "auth", "credential"]):
                    line_redacted = re.sub(r'([A-Za-z0-9_\-\.\=\+\/]{12,})', '[REDACTED]', line_redacted)
                errs_warnings.append((log_name, idx, line_redacted))
    except Exception as e:
        pass

# Sort they are chronological, and get the last 5
# To be robust, let's keep them ordered by log and index or just last 5 from the list (which naturally orders them chronologically if we read logs in sequence or combine them).
# Let's show the final 5 chronologically if possible, or 5 latest from apextrader.log and autobot.log combined.
# Since we process autobot then apextrader, let's just reverse and read from end of files to get the absolute last 5 events.
combined_recent = []
for log_name in logs:
    try:
        with open(log_name, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        for idx in range(len(lines) - 1, -1, -1):
            line = lines[idx]
            line_upper = line.upper()
            if "ERROR" in line_upper or "WARNING" in line_upper or "TRACEBACK" in line_upper or "EXCEPTION" in line_upper or "FAILED" in line_upper:
                line_redacted = line.strip()
                if any(sec in line_redacted.lower() for sec in ["token", "key", "secret", "password", "auth", "credential"]):
                    line_redacted = re.sub(r'([A-Za-z0-9_\-\.\=\+\/]{12,})', '[REDACTED]', line_redacted)
                combined_recent.append((log_name, idx, line_redacted))
                if len(combined_recent) >= 20: # grab enough to sort/filter
                    break
    except Exception:
        pass

# Let's print the last 5 we found
print("Last 5 errors/warnings:")
for item in combined_recent[:5]:
    print(f"[{item[0]} Line {item[1]}]: {item[2]}")
