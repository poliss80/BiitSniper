import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date
from typing import List, Dict, Optional
import hashlib
import os
import time


from engine.utils import bool_env, get_env, format_currency

from engine.config import (
    USE_EMAIL_NOTIFICATIONS,
    EMAIL_SMTP_SERVER,
    EMAIL_SMTP_PORT,
    EMAIL_SMTP_USER,
    EMAIL_SMTP_PASSWORD,
    EMAIL_FROM_ADDRESS,
    EMAIL_TO_ADDRESSES,
    EMAIL_SCAN_MIN_INTERVAL_SEC,
    EMAIL_SCAN_SEND_ON_CHANGE,
)


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def _get_env(name: str, default="") -> str:
    return os.getenv(name, default).strip()


def _format_currency(value: float) -> str:
    return f"${value:,.2f}"


def _position_number(position, attribute: str) -> float:
    return float(getattr(position, attribute, 0) or 0)


def _format_signal_text(signals) -> str:
    if not signals:
        return "No signals"
    lines = []
    for i, s in enumerate(signals[:3], start=1):
        lines.append(f"#{i}: {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] {s.reason}")
    return "\n".join(lines)


def _format_signal_html(signals) -> str:
    if not signals:
        return "<p>No signals</p>"
    rows = "".join(
        f"<tr><td>{i+1}</td><td>{s.symbol}</td><td>{s.action.upper()}</td><td>${s.price:.2f}</td><td>{s.confidence:.0%}</td><td>{s.strategy}</td><td>{s.reason}</td></tr>"
        for i, s in enumerate(signals[:3])
    )
    return (
        "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;'>"
        "<thead><tr><th>#</th><th>Symbol</th><th>Action</th><th>Price</th><th>Confidence</th><th>Strategy</th><th>Reason</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _build_html_section(title: str, content: str) -> str:
    return f"<h2>{title}</h2>\n{content}\n"


def _build_signal_table(signals) -> str:
    if not signals:
        return "<p>No signals available.</p>"

    rows = "".join(
        f"<tr><td>{i+1}</td><td>{s.symbol}</td><td>{s.action.upper()}</td><td>${s.price:.2f}</td><td>{s.confidence:.0%}</td><td>{s.strategy}</td><td>{s.reason}</td></tr>"
        for i, s in enumerate(signals)
    )
    return (
        "<table border='1' cellpadding='5' cellspacing='0' style='border-collapse:collapse;width:100%;'>"
        "<thead><tr><th>#</th><th>Symbol</th><th>Action</th><th>Price</th><th>Confidence</th><th>Strategy</th><th>Reason</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _build_positions_table(positions) -> str:
    if not positions:
        return "<p>No open positions at EOD.</p>"

    sorted_positions = sorted(positions, key=lambda p: _position_number(p, "unrealized_pl"), reverse=True)
    rows = "".join(
        f"<tr><td>{p.symbol}</td><td>{p.qty}</td><td>${_position_number(p, 'avg_entry_price'):,.2f}</td>"
        f"<td>{_position_number(p, 'current_price'):,.2f}</td><td>{_position_number(p, 'unrealized_pl'):,.2f}</td>"
        f"<td>{_position_number(p, 'unrealized_plpc') * 100:.2f}%</td></tr>"
        for p in sorted_positions
    )
    return (
        "<table border='1' cellpadding='5' cellspacing='0' style='border-collapse:collapse;'>"
        "<thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Unrealized P&L</th><th>Unrealized %</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def build_top5_report(signals, report_date: date, sentiment: str = "neutral",
                      regime: str = "bull") -> Dict[str, str]:
    action_counts = {"buy": 0, "sell": 0, "short": 0}
    for s in signals[:5]:
        action_counts[s.action.lower()] = action_counts.get(s.action.lower(), 0) + 1
    top_conf = max((s.confidence for s in signals[:5]), default=0.0)
    subject = (
        f"\U0001f4c8 ApexTrader {regime.upper()} {sentiment.upper()} | "
        f"{len(signals[:5])} picks | top {top_conf:.0%} \u2014 {report_date.strftime('%b %d, %Y')}"
    )

    # Plain text
    text_lines = [
        f"ApexTrader Top 5 Scan Picks — {report_date.isoformat()}",
        f"Market Sentiment: {sentiment.upper()} | Regime: {regime.upper()}",
        (
            f"Action Mix: BUY {action_counts.get('buy', 0)} | "
            f"SHORT {action_counts.get('short', 0)} | "
            f"SELL {action_counts.get('sell', 0)}"
        ),
        "",
    ]
    text_lines.append(_format_signal_text(signals))
    text = "\n".join(text_lines)

    # Colours & icons
    _sent_color = {"bullish": "#16a34a", "bearish": "#dc2626", "neutral": "#d97706"}.get(sentiment.lower(), "#6b7280")
    _sent_icon  = {"bullish": "\U0001f7e2", "bearish": "\U0001f534", "neutral": "\U0001f7e1"}.get(sentiment.lower(), "\u26aa")
    _regime_color = "#16a34a" if regime.lower() == "bull" else "#dc2626"
    _regime_icon  = "\U0001f4c8" if regime.lower() == "bull" else "\U0001f4c9"

    # Strategy insight blurbs
    _strat_insight = {
        "TrendBreaker":    "Shorts trapped above key MA — squeeze in progress",
        "Sweepea":         "Liquidity swept below support, pinbar reversal forming",
        "GapBreakout":     "Gap-up continuation — momentum carrying overnight move",
        "ORB":             "Opening range cleared — intraday breakout confirmed",
        "VWAPReclaim":     "VWAP reclaimed with volume — institutional buying",
        "FloatRotation":   "Low float rotating fast — high short interest catalyst",
        "Momentum":        "Strong price momentum with volume surge backing it",
        "Technical":       "Multi-indicator confluence — RSI, MACD, MA aligned",
    }

    medals = ["\U0001f947", "\U0001f948", "\U0001f949", "\U0001f3c5", "\U0001f396\ufe0f"]
    action_colors = {"buy": "#10b981", "sell": "#f43f5e", "short": "#f43f5e"}

    cards = ""
    for i, s in enumerate(signals[:5]):
        medal  = medals[i] if i < len(medals) else f"#{i+1}"
        acolor = action_colors.get(s.action.lower(), "#3b82f6")
        conf_pct = int(s.confidence * 100)
        insight  = _strat_insight.get(s.strategy, s.reason[:60])

        # ATR stop line
        if getattr(s, "atr_stop", None):
            stop_price = round(s.price - s.atr_stop, 2)
            tp_price   = round(s.price + s.atr_stop * 2, 2)
            risk_line  = f"Stop ~${stop_price:.2f} &nbsp;·&nbsp; Target ~${tp_price:.2f} &nbsp;·&nbsp; R/R 1:2"
        else:
            stop_price = round(s.price * 0.97, 2)
            risk_line  = f"Stop ~${stop_price:.2f} (3% default)"

        cards += f"""
        <div style="background:#ffffff;border-radius:0;padding:16px 24px;margin-bottom:0;border-left:4px solid {acolor};border-bottom:1px solid #f0f2f5;">
          <!-- Symbol row -->
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
            <span style="font-size:18px;font-weight:800;color:#1a1a2e;letter-spacing:0.5px;">{medal} {s.symbol}</span>
            <span style="background:{acolor};color:#fff;padding:4px 13px;border-radius:20px;font-size:11px;font-weight:800;letter-spacing:1px;">{s.action.upper()}</span>
          </div>
          <!-- Stats row -->
          <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;">
            <div style="background:#f5f7fa;border-radius:5px;padding:6px 12px;text-align:center;border:1px solid #e8eaed;">
              <div style="color:#888;font-size:9px;letter-spacing:1px;text-transform:uppercase;">Entry</div>
              <div style="color:#1a1a2e;font-size:14px;font-weight:700;">${s.price:.2f}</div>
            </div>
            <div style="background:#f5f7fa;border-radius:5px;padding:6px 12px;text-align:center;border:1px solid #e8eaed;">
              <div style="color:#888;font-size:9px;letter-spacing:1px;text-transform:uppercase;">Confidence</div>
              <div style="color:{acolor};font-size:14px;font-weight:700;">{conf_pct}%</div>
            </div>
            <div style="background:#f5f7fa;border-radius:5px;padding:6px 12px;text-align:center;border:1px solid #e8eaed;">
              <div style="color:#888;font-size:9px;letter-spacing:1px;text-transform:uppercase;">Strategy</div>
              <div style="color:#2563eb;font-size:12px;font-weight:700;">{s.strategy}</div>
            </div>
          </div>
          <!-- Confidence bar -->
          <div style="margin-bottom:10px;">
            <div style="background:#e5e7eb;border-radius:999px;height:4px;width:100%;overflow:hidden;">
              <div style="background:{acolor};height:4px;border-radius:999px;width:{conf_pct}%;"></div>
            </div>
          </div>
          <!-- Insight -->
          <div style="background:#f5f7fa;border-radius:5px;padding:8px 12px;margin-bottom:8px;border:1px solid #e8eaed;">
            <div style="color:#555;font-size:11px;line-height:1.5;">💡 {insight}</div>
          </div>
          <!-- Risk line -->
          <div style="color:#888;font-size:10px;padding-top:2px;">⚡ {risk_line}</div>
          <!-- Raw reason -->
          <div style="color:#aaa;font-size:10px;margin-top:4px;font-style:italic;">{s.reason}</div>
        </div>"""

    if not signals:
        cards = "<div style='padding:28px;text-align:center;color:#999;font-size:13px;'>No qualifying signals this scan cycle.</div>"

    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:'Segoe UI',Arial,sans-serif;color:#1a1a2e;">
  <div style="max-width:580px;margin:20px auto;background:#ffffff;border-radius:8px;border:1px solid #d8dde4;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.07);">

    <!-- Header -->
    <div style="background:#1a1a2e;padding:20px 24px 16px;text-align:center;">
      <div style="font-size:10px;color:#7b8fa3;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px;">ApexTrader &nbsp;·&nbsp; Automated Scan</div>
      <div style="font-size:24px;font-weight:900;color:#f8fafc;">📈 Top Picks</div>
      <div style="font-size:12px;color:#7b8fa3;margin-top:3px;">{report_date.strftime('%A, %B %d, %Y')}</div>
    </div>

    <!-- Regime bar -->
    <div style="background:#f8f9fa;padding:9px 24px;border-bottom:1px solid #e0e4ea;display:flex;align-items:center;gap:8px;font-size:11px;">
      <span style="background:{_regime_color};color:#fff;padding:2px 9px;border-radius:3px;font-size:10px;font-weight:700;">{regime.upper()}</span>
      <span style="background:{_sent_color};color:#fff;padding:2px 9px;border-radius:3px;font-size:10px;font-weight:700;">{sentiment.upper()}</span>
      <span style="margin-left:auto;color:#888;">BUY {action_counts.get('buy', 0)} / SHORT {action_counts.get('short', 0)} &nbsp;·&nbsp; Top: {top_conf:.0%}</span>
    </div>

    <!-- Cards -->
    <div style="background:#ffffff;padding:8px 0 16px;">
      {cards}
    </div>

    <!-- Footer -->
    <div style="text-align:center;background:#f8f9fa;padding:10px 24px;border-top:1px solid #e0e4ea;">
      <span style="color:#aaa;font-size:10px;">ApexTrader &nbsp;·&nbsp; {report_date.isoformat()}</span>
    </div>

  </div>
</body></html>"""

    return {"subject": subject, "text": text, "html": html}


def build_eod_report(
    report_date: date,
    market_summary: str,
    account_summary: Dict,
    daily_pnl: float,
    total_trades: int,
    eod_close_summary: Dict,
    positions,
    discovery_tickers: Optional[List[Dict]] = None,
) -> Dict[str, str]:
    # Plain text summary
    txt = []
    txt.append(f"ApexTrader EOD Report - {report_date.isoformat()}")
    txt.append("=" * 60)
    txt.append(f"Market Summary: {market_summary}")
    txt.append("")
    txt.append("Account:")
    txt.append(f"- Equity: {account_summary.get('equity')}")
    txt.append(f"- Cash/Buying Power: {account_summary.get('buying_power')}")
    txt.append("")
    txt.append(f"Daily P&L: {_format_currency(daily_pnl)}")
    txt.append(f"Trades Today: {total_trades}")
    txt.append("")
    txt.append("EOD Close Summary:")
    txt.append(f"- Closed positions: {eod_close_summary.get('closed_count', 0)}")
    txt.append(f"- Failed closes: {eod_close_summary.get('failed_count', 0)}")
    txt.append("")
    if eod_close_summary.get("closed_items"):
        txt.append("Closed Symbols:")
        for item in eod_close_summary["closed_items"]:
            txt.append(
                f"  - {item['symbol']}: {item['qty']} sh | Strategy {item['strategy']} | P&L {_format_currency(item['pnl'])}"
            )
    else:
        txt.append("No EOD close trades.")

    txt.append("")
    txt.append("Open Positions:")
    if positions:
        sorted_positions = sorted(positions, key=lambda p: _position_number(p, "unrealized_pl"), reverse=True)
        for p in sorted_positions:
            txt.append(
                f"  - {p.symbol}: {p.qty} sh @ ${_position_number(p, 'avg_entry_price'):,.2f} | "
                f"Unrealized {_format_currency(_position_number(p, 'unrealized_pl'))} ({_position_number(p, 'unrealized_plpc')*100:.2f}%)"
            )
    else:
        txt.append("  - None")

    txt.append("")
    txt.append("Flashy Insights:")
    top_open = sorted(positions, key=lambda p: _position_number(p, "unrealized_pl"), reverse=True)[0] if positions else None
    bottom_open = sorted(positions, key=lambda p: _position_number(p, "unrealized_pl"), reverse=False)[0] if positions else None
    txt.append(f"- Open positions: {len(positions)}")
    txt.append(f"- EOD closed trades: {eod_close_summary.get('closed_count',0)}")
    txt.append(f"- Daily P&L: {_format_currency(daily_pnl)}")
    if top_open:
        txt.append(f"- Top gainer: {top_open.symbol} {_format_currency(_position_number(top_open, 'unrealized_pl'))} ({_position_number(top_open, 'unrealized_plpc')*100:.2f}%)")
    if bottom_open:
        txt.append(f"- Top loser: {bottom_open.symbol} {_format_currency(_position_number(bottom_open, 'unrealized_pl'))} ({_position_number(bottom_open, 'unrealized_plpc')*100:.2f}%)")

    txt.append("")
    txt.append("Latest Scan Candidates:")
    if discovery_tickers:
        sorted_candidates = sorted(discovery_tickers, key=lambda t: float(t.get('momentum_pct', 0)), reverse=True)
        for t in sorted_candidates[:12]:
            symbol = t.get('symbol', 'N/A')
            mp = float(t.get('momentum_pct', 0))
            cp = float(t.get('current_price', 0)) if t.get('current_price') is not None else 0
            sentiment = t.get('sentiment', 'n/a')
            txt.append(f"  - {symbol}: momentum={mp:+.1f}% | price=${cp:,.2f} | sentiment={sentiment}")
    else:
        txt.append("  - none (no live discovery data)")

    # HTML summary
    subject_prefix = _get_env('EMAIL_SUBJECT_PREFIX', 'ApexTrader EOD Report')
    html = ["<html><body style='font-family:Arial,Helvetica,sans-serif;background:#eaf1f8;margin:0;padding:20px;color:#1b2541;'>"]
    html.append("<div style='max-width:820px;margin:0 auto;padding:20px;background:linear-gradient(145deg,#f7fbff,#dbefff);border-radius:18px;box-shadow:0 8px 26px rgba(14,33,77,0.16);border:1px solid rgba(71,114,177,0.2);'>")
    html.append(f"<h1 style='margin:0;color:#0d1b2a;font-size:24px;'>{subject_prefix} - {report_date.isoformat()}</h1>")
    html.append("<p style='margin:8px 0 20px;color:#2f4f4f;font-size:14px;'>Daily briefing with account status, trade activity, and EOD closing actions.</p>")

    html.append("<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:22px;'>")
    html.append(f"<div style='background:#ffffffc0;border-left:4px solid #3c72c5;padding:14px;border-radius:12px;box-shadow:0 2px 10px rgba(46,86,139,0.12);'><strong>Market</strong><br>{market_summary}</div>")
    html.append(f"<div style='background:#ffffffc0;border-left:4px solid #2a9365;padding:14px;border-radius:12px;box-shadow:0 2px 10px rgba(28,108,67,0.12);'><strong>Equity</strong><br>{_format_currency(float(account_summary.get('equity',0)))}</div>")
    html.append(f"<div style='background:#ffffffc0;border-left:4px solid #d39500;padding:14px;border-radius:12px;box-shadow:0 2px 10px rgba(121,89,0,0.12);'><strong>Buying Power</strong><br>{_format_currency(float(account_summary.get('buying_power',0)))}</div>")
    html.append("</div>")

    # Add flashy insights block
    html.append("<div style='background:linear-gradient(135deg, #1d3f72, #2a65a3);border-radius:12px;padding:16px;color:#f7fbff;margin-bottom:20px;box-shadow:0 6px 18px rgba(19,55,103,0.2);'>")
    html.append("<h3 style='margin:0;font-size:18px;'>✨ Market Pulse</h3>")
    html.append(f"<p style='margin:8px 0;line-height:1.45;font-size:14px;'>Today: {market_summary}. Position count: <strong>{len(positions)}</strong>.</p>")
    html.append("<p style='margin:6px 0 4px;font-size:13px;color:#c7daff;'>Outlook: continue tracking momentum leaders; watch for reversals around key round levels.</p>")
    html.append("<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;'>")
    html.append(f"<span style='background:#ffffff26;padding:5px 10px;border-radius:8px;font-size:12px;'>Open positions: {len(positions)}</span>")
    html.append(f"<span style='background:#ffffff26;padding:5px 10px;border-radius:8px;font-size:12px;'>EOD closes: {eod_close_summary.get('closed_count',0)}</span>")
    html.append(f"<span style='background:#ffffff26;padding:5px 10px;border-radius:8px;font-size:12px;'>Daily P&L: {_format_currency(daily_pnl)}</span>")
    html.append("</div>")
    html.append("</div>")

    html.append("<div style='background:#fff8f2;padding:14px;border-radius:10px;margin-bottom:20px;border:1px solid #ffdab9;'>")
    html.append(f"<h3 style='margin:0 0 8px;font-size:16px;color:#a14104;'>Daily Performance</h3>")
    html.append(f"<p style='margin:0; font-size:14px; color:#6b3b00;'>Daily P&L: <strong>{_format_currency(daily_pnl)}</strong><br>Trades Today: <strong>{total_trades}</strong></p>")
    html.append("</div>")

    # Flashy insights summary
    top_open = sorted(positions, key=lambda p: _position_number(p, "unrealized_pl"), reverse=True)[0] if positions else None
    bottom_open = sorted(positions, key=lambda p: _position_number(p, "unrealized_pl"), reverse=False)[0] if positions else None
    total_open = len(positions)
    closed = eod_close_summary.get('closed_count', 0)

    html.append("<div style='background:linear-gradient(135deg, #ff6b6b, #fca311);color:#fff;padding:14px;border-radius:12px;margin-bottom:20px;border:1px solid #e2962d;'>")
    html.append("<h3 style='margin:0 0 8px;font-size:16px;'>🚀 Flashy Insights</h3>")
    html.append(f"<p style='margin:0;font-size:13px;'>Open positions: <strong>{total_open}</strong> | EOD closes: <strong>{closed}</strong> | Daily P&L: <strong>{_format_currency(daily_pnl)}</strong></p>")
    if top_open:
        html.append(f"<p style='margin:6px 0 0;font-size:13px;'>Top gainer: <strong>{top_open.symbol}</strong> {_format_currency(_position_number(top_open, 'unrealized_pl'))} ({_position_number(top_open, 'unrealized_plpc')*100:.2f}%)</p>")
    if bottom_open:
        html.append(f"<p style='margin:6px 0 0;font-size:13px;'>Top loser: <strong>{bottom_open.symbol}</strong> {_format_currency(_position_number(bottom_open, 'unrealized_pl'))} ({_position_number(bottom_open, 'unrealized_plpc')*100:.2f}%)</p>")
    html.append("</div>")

    html.append("<div style='background:linear-gradient(135deg, #27496d, #3a86ff);color:#fdfdff;padding:14px;border-radius:14px;border:1px solid #4d7fc9;margin-bottom:16px; box-shadow:0 4px 18px rgba(35,102,191,0.2);'>")
    html.append("<h3 style='margin:0 0 8px;font-size:16px;'>🔥 Latest Scrape/Discovery Candidates</h3>")
    if discovery_tickers:
        html.append("<ul style='margin:0;padding-left:20px;color:#f1f5ff;font-size:13px;'>")
        sorted_candidates = sorted(discovery_tickers, key=lambda t: float(t.get('momentum_pct', 0)), reverse=True)
        for t in sorted_candidates[:12]:
            symbol = t.get('symbol', 'N/A')
            mp = float(t.get('momentum_pct', 0))
            cp = float(t.get('current_price', 0)) if t.get('current_price') is not None else 0
            sentiment = t.get('sentiment', 'n/a')
            html.append(f"<li style='margin-bottom:4px;'><strong>{symbol}</strong> — {mp:+.1f}% | ${cp:,.2f} | sentiment: {sentiment}</li>")
        html.append("</ul>")
    else:
        html.append("<p style='margin:0;color:#c5d6ff;font-size:13px;'>No recent discovery tickers available.</p>")
    html.append("</div>")

    # Fancy position table
    html.append("<div style='margin-bottom:20px;'><h3 style='font-size:16px;color:#1a3960;margin-bottom:10px;'>Open Positions</h3>")
    html.append("<table style='width:100%;border-collapse:separate;border-spacing:0 4px;font-size:13px;line-height:1.4;background:#f8fbff;border-radius:10px;overflow:hidden;'>")
    html.append("<thead style='background:#e0ecfc;color:#0f3057;'><tr><th style='padding:10px 12px;border:none;text-align:left;'>Symbol</th><th style='padding:10px 12px;border:none;text-align:right;'>Qty</th><th style='padding:10px 12px;border:none;text-align:right;'>Entry</th><th style='padding:10px 12px;border:none;text-align:right;'>Current</th><th style='padding:10px 12px;border:none;text-align:right;'>P/L</th><th style='padding:10px 12px;border:none;text-align:right;'>P/L %</th></tr></thead>")
    if positions:
        sorted_positions = sorted(positions, key=lambda p: _position_number(p, "unrealized_pl"), reverse=True)
        html.append("<tbody>")
        for idx, p in enumerate(sorted_positions):
            row_bg = '#fdfdff' if idx % 2 == 0 else '#f4f8ff'
            pl_value = _position_number(p, "unrealized_pl")
            pl_color = '#118032' if pl_value >= 0 else '#b00000'
            val_pct = _position_number(p, "unrealized_plpc") * 100
            html.append(f"<tr style='background:{row_bg};'><td style='padding:8px;border:1px solid #e2e9f9;'>{p.symbol}</td><td style='padding:8px;border:1px solid #e2e9f9;'>{p.qty}</td><td style='padding:8px;border:1px solid #e2e9f9;'>${_position_number(p, 'avg_entry_price'):,.2f}</td><td style='padding:8px;border:1px solid #e2e9f9;'>${_position_number(p, 'current_price'):,.2f}</td><td style='padding:8px;border:1px solid #e2e9f9;color:{pl_color};font-weight:600;'>{_format_currency(pl_value)}</td><td style='padding:8px;border:1px solid #e2e9f9;color:{pl_color};font-weight:600;'>{val_pct:.2f}%</td></tr>")
        html.append("</tbody>")
    else:
        html.append("<tbody><tr><td colspan='6' style='padding:8px;border:1px solid #e2e9f9;text-align:center;color:#667089;'>No open positions</td></tr></tbody>")
    html.append("</table></div>")

    eod_html_lines = [f"<li>{i['symbol']} ({i['qty']} sh, {i['strategy']}) P&L {_format_currency(i['pnl'])}</li>" for i in eod_close_summary.get('closed_items', [])]
    if eod_html_lines:
        eod_html = "<ul>" + "".join(eod_html_lines) + "</ul>"
    else:
        eod_html = "<p>No EOD close trades</p>"
    html.append(_build_html_section("EOD Close Trades", eod_html))

    # Avoid duplicate open positions table; already included above in fancy section
    html.append("</body></html>")

    subject = f"{subject_prefix} - {report_date.isoformat()}"
    return {"subject": subject, "text": "\n".join(txt), "html": "\n".join(html)}


def send_email(subject: str, text: str, html: Optional[str] = None) -> bool:
    if not _bool_env('USE_EMAIL_NOTIFICATIONS', 'false') and not USE_EMAIL_NOTIFICATIONS:
        return False

    env_to = [a.strip() for a in _get_env('EMAIL_TO_ADDRESSES', '').split(',') if a.strip()]
    to_addresses = env_to or EMAIL_TO_ADDRESSES
    if not to_addresses:
        raise ValueError("No EMAIL_TO_ADDRESSES configured")

    from_address = _get_env('EMAIL_FROM_ADDRESS', EMAIL_FROM_ADDRESS or EMAIL_SMTP_USER)
    if not from_address:
        raise ValueError("No EMAIL_FROM_ADDRESS configured")

    smtp_server = _get_env('EMAIL_SMTP_SERVER', EMAIL_SMTP_SERVER or 'smtp.gmail.com')
    smtp_port = int(_get_env('EMAIL_SMTP_PORT', str(EMAIL_SMTP_PORT or 587)))
    smtp_user = _get_env('EMAIL_SMTP_USER', EMAIL_SMTP_USER)
    smtp_pass = _get_env('EMAIL_SMTP_PASSWORD', EMAIL_SMTP_PASSWORD)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = from_address
    msg['To'] = ', '.join(to_addresses)

    msg.attach(MIMEText(text, 'plain'))
    if html:
        msg.attach(MIMEText(html, 'html'))

    # Run the SMTP session on a daemon thread capped at 60 s total.
    # This prevents the trading loop from blocking when the mail server
    # is slow, unreachable, or stuck in a TLS handshake.
    import threading as _t
    _result: list = [None]
    _exc:    list = [None]

    def _smtp_send():
        try:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            try:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(from_address, to_addresses, msg.as_string())
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            _result[0] = True
        except Exception as e:
            _exc[0] = e

    _th = _t.Thread(target=_smtp_send, daemon=True)
    _th.start()
    _th.join(timeout=60)

    if _th.is_alive():
        # Thread still blocked after 60 s — log and return; trading loop continues.
        print(f"[WARN ] send_email timed out (>60 s) for subject: {subject!r}")
        return False
    if _exc[0] is not None:
        raise _exc[0]
    return bool(_result[0])


# ── High-level notification helpers ───────────────────────────────────────
import logging as _logging
_nlog = _logging.getLogger("ApexTrader")
_last_scan_sent_at: float = 0.0
_last_scan_fingerprint: str = ""

# Per-ticker suppression: symbol -> timestamp of last time it was in a sent email.
# Prevents repeat-notifying the same ticker within NOTIFIED_TICKER_SUPPRESS_SEC.
_notified_tickers: dict[str, float] = {}
NOTIFIED_TICKER_SUPPRESS_SEC = int(os.getenv("NOTIFIED_TICKER_SUPPRESS_SEC", str(3 * 3600)))  # 3 hrs


def _has_fresh_ticker(picks, now: float) -> bool:
    """Return True if at least one pick has NOT been notified within the suppress window."""
    for s in picks[:5]:
        last = _notified_tickers.get(s.symbol, 0.0)
        if now - last >= NOTIFIED_TICKER_SUPPRESS_SEC:
            return True
    return False


def _record_notified_tickers(picks, now: float) -> None:
    for s in picks[:5]:
        _notified_tickers[s.symbol] = now


def _scan_fingerprint(picks, sentiment: str, regime: str) -> str:
    core = [(s.symbol, s.action, round(float(s.confidence), 2), s.strategy) for s in picks[:5]]
    payload = f"{regime}|{sentiment}|{core}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def notify_scan_results(signals, report_date, sentiment: str, regime: str,
                        subject_prefix: str = "") -> bool:
    """Send top-5 scan picks email. Returns True if sent, False otherwise."""
    global _last_scan_sent_at, _last_scan_fingerprint
    picks = list(signals)[:5]
    if not picks:
        _nlog.info("notify_scan_results: no picks to send")
        return False
    now = time.time()
    fp = _scan_fingerprint(picks, sentiment, regime)
    age = now - _last_scan_sent_at if _last_scan_sent_at else float("inf")

    if fp == _last_scan_fingerprint and age < EMAIL_SCAN_MIN_INTERVAL_SEC:
        _nlog.info(f"Scan email throttled: unchanged picks ({age:.0f}s < {EMAIL_SCAN_MIN_INTERVAL_SEC}s)")
        return False

    # Suppress if every ticker was already notified within the 3-hr window.
    if not _has_fresh_ticker(picks, now):
        syms = [s.symbol for s in picks[:5]]
        _nlog.info(f"Scan email suppressed: all top-5 tickers ({syms}) already notified within {NOTIFIED_TICKER_SUPPRESS_SEC//3600}h")
        return False

    # Hard minimum interval: never send more often than this, even if picks changed.
    # This prevents scan-cycle spam during fast-moving markets.
    # For changed picks, still enforce a meaningful cool-down window to avoid
    # near-every-cycle emails in choppy sessions.
    min_change_interval = max(300, EMAIL_SCAN_MIN_INTERVAL_SEC)
    if age < EMAIL_SCAN_MIN_INTERVAL_SEC:
        changed = fp != _last_scan_fingerprint
        if not (EMAIL_SCAN_SEND_ON_CHANGE and changed and age >= min_change_interval):
            _nlog.info(
                "Scan email throttled: "
                f"age={age:.0f}s, changed={changed}, min={EMAIL_SCAN_MIN_INTERVAL_SEC}s"
            )
            return False

    try:
        report = build_top5_report(picks, report_date, sentiment, regime)
        if subject_prefix:
            report["subject"] = subject_prefix + report["subject"]
        sent   = send_email(report["subject"], report["text"], report["html"])
        if sent:
            _last_scan_sent_at = now
            _last_scan_fingerprint = fp
            _record_notified_tickers(picks, now)
        _nlog.info("Scan email sent" if sent else "Scan email skipped (disabled)")
        return bool(sent)
    except Exception as e:
        _nlog.warning(f"Scan email failed: {e}")
        return False


def notify_eod(
    eod_close_summary: Dict,
    account,
    positions,
    daily_pnl: float,
    total_trades: int,
    discovery_tickers=None,
) -> bool:
    """Send EOD report email. Returns True if sent, False otherwise."""
    try:
        report = build_eod_report(
            report_date     = date.today(),
            market_summary  = str(getattr(account, "status", "open")),
            account_summary = {
                "equity":       float(account.equity),
                "buying_power": float(account.buying_power),
                "pdt_protected": getattr(account, "pattern_day_trader", False),
            },
            daily_pnl          = daily_pnl,
            total_trades       = total_trades,
            eod_close_summary  = eod_close_summary,
            positions          = positions,
            discovery_tickers  = discovery_tickers or [],
        )
        sent = send_email(report["subject"], report["text"], report["html"])
        _nlog.info("EOD email sent" if sent else "EOD email skipped (disabled)")
        return bool(sent)
    except Exception as e:
        _nlog.error(f"EOD email failed: {e}")
        return False
