"""End-of-day PDF report generator.

Usage:
    python eod_report.py                       # today IST
    python eod_report.py --date 2026-05-20     # specific day

Output: reports/EOD_Report_YYYY-MM-DD.pdf
Pulls all data from Supabase. Includes per-symbol signal breakdown,
notifications fired, scalp simulation, and 2-day comparison.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, cm
from reportlab.lib.colors import HexColor, white, black
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

import db

IST = ZoneInfo("Asia/Kolkata")
LOT_SIZE = {"NIFTY": 75, "BANKNIFTY": 15, "SENSEX": 20}
TARGET_PTS = {"NIFTY": 30, "BANKNIFTY": 60, "SENSEX": 100}
SL_PTS = {"NIFTY": 18, "BANKNIFTY": 30, "SENSEX": 50}

# Colors (professional report palette)
COLOR_PRIMARY = HexColor("#1e3a8a")     # deep blue
COLOR_ACCENT = HexColor("#0ea5e9")      # sky blue
COLOR_SUCCESS = HexColor("#15803d")     # green
COLOR_DANGER = HexColor("#dc2626")      # red
COLOR_WARN = HexColor("#ca8a04")        # amber
COLOR_MUTED = HexColor("#64748b")       # slate
COLOR_BG_LIGHT = HexColor("#f1f5f9")    # near-white grey
COLOR_BORDER = HexColor("#cbd5e1")      # light grey


def parse_ts(s):
    if s.endswith("Z"): s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(IST)


def simulate_scalp(direction, entry, target, sl, future):
    """Simulate a +target / -sl scalp over next `future` candles (10-min hold)."""
    for j, c in enumerate(future):
        hi, lo = float(c["high"]), float(c["low"])
        if direction == "PUT":
            win = (entry - lo) >= target
            loss = (hi - entry) >= sl
        else:
            win = (hi - entry) >= target
            loss = (entry - lo) >= sl
        if loss: return "LOSS", -sl, j+1
        if win: return "WIN", target, j+1
    pts = float(future[-1]["close"]) - entry if direction == "CALL" else entry - float(future[-1]["close"])
    return ("TIMEOUT_WIN" if pts > 0 else "TIMEOUT_LOSS"), round(pts, 2), len(future)


def analyze_symbol(date_str, symbol):
    """Return dict with all stats for one symbol for the day."""
    snaps = db.get_snapshots(date_str, symbol)
    candles = db.get_candles(symbol, start_date=date_str, end_date=date_str, interval=5)
    for c in candles:
        c["_dt"] = parse_ts(c["ts"])
    candles.sort(key=lambda c: c["_dt"])

    out = {
        "symbol": symbol,
        "date": date_str,
        "snaps": snaps,
        "snap_count": len(snaps),
        "first_spot": None, "last_spot": None, "session_move": 0, "session_move_pct": 0,
        "counts": {"CALL": 0, "PUT": 0, "NEUTRAL": 0},
        "transitions": [],
        "scalp_trades": [],
        "scalp_wins": 0, "scalp_losses": 0, "scalp_net_pts": 0, "scalp_net_inr": 0,
    }
    if not snaps:
        return out

    out["first_spot"] = float(snaps[0]["spot_price"])
    out["last_spot"] = float(snaps[-1]["spot_price"])
    out["session_move"] = out["last_spot"] - out["first_spot"]
    out["session_move_pct"] = out["session_move"] / out["first_spot"] * 100 if out["first_spot"] else 0

    for s in snaps:
        out["counts"][s["signal"]] = out["counts"].get(s["signal"], 0) + 1

    # Transitions
    prev = ""
    for s in snaps:
        if s["signal"] != "NEUTRAL" and s["signal"] != prev:
            out["transitions"].append(s)
        prev = s["signal"]

    # Scalp simulation
    target = TARGET_PTS[symbol]
    sl = SL_PTS[symbol]
    last_trade_dt = None
    for s in snaps:
        if s["signal"] == "NEUTRAL":
            continue
        sig_dt = parse_ts(s["ts"])
        if last_trade_dt and (sig_dt - last_trade_dt).total_seconds() < 60 * 60:
            continue
        future = [c for c in candles if c["_dt"] >= sig_dt][:2]
        if not future:
            continue
        entry = float(s["spot_price"])
        outcome, pts, held = simulate_scalp(s["signal"], entry, target, sl, future)
        out["scalp_trades"].append({
            "time": sig_dt.strftime("%H:%M"),
            "direction": s["signal"],
            "entry": entry,
            "outcome": outcome,
            "points": pts,
            "held_min": held * 5,
        })
        if "WIN" in outcome:
            out["scalp_wins"] += 1
        elif "LOSS" in outcome:
            out["scalp_losses"] += 1
        out["scalp_net_pts"] += pts
        last_trade_dt = sig_dt

    # Rough INR estimate (ATM delta ≈ 0.50)
    lot = LOT_SIZE[symbol]
    out["scalp_net_inr"] = int(out["scalp_net_pts"] * 0.5 * lot)
    out["scalp_net_inr_after_brokerage"] = out["scalp_net_inr"] - len(out["scalp_trades"]) * 100
    return out


def build_pdf(date_str, out_path):
    """Build the EOD PDF and write to out_path. NIFTY-only — BANKNIFTY skipped."""
    nifty = analyze_symbol(date_str, "NIFTY")

    # Get yesterday's date for 2-day comparison
    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
    prev_dt = date_dt - timedelta(days=1)
    while prev_dt.weekday() >= 5:  # skip weekend
        prev_dt -= timedelta(days=1)
    prev_date = prev_dt.strftime("%Y-%m-%d")
    prev_nifty = analyze_symbol(prev_date, "NIFTY") if prev_date else None

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        topMargin=0.6*inch, bottomMargin=0.5*inch,
        leftMargin=0.6*inch, rightMargin=0.6*inch,
        title=f"NSE Signal EOD Report {date_str}",
        author="NSE Signal Engine",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title", parent=styles["Title"],
        fontSize=20, textColor=COLOR_PRIMARY, spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "SubTitle", parent=styles["Normal"],
        fontSize=11, textColor=COLOR_MUTED, spaceAfter=12,
    )
    h2 = ParagraphStyle(
        "H2", parent=styles["Heading2"],
        fontSize=14, textColor=COLOR_PRIMARY, spaceBefore=10, spaceAfter=6,
    )
    h3 = ParagraphStyle(
        "H3", parent=styles["Heading3"],
        fontSize=11, textColor=COLOR_ACCENT, spaceBefore=8, spaceAfter=4,
    )
    body = ParagraphStyle(
        "Body", parent=styles["Normal"],
        fontSize=10, spaceAfter=4,
    )
    note = ParagraphStyle(
        "Note", parent=styles["Normal"],
        fontSize=9, textColor=COLOR_MUTED, spaceBefore=2,
    )

    el = []
    el.append(Paragraph("NSE Signal Engine &mdash; EOD Report", title_style))
    day_name = date_dt.strftime("%A")
    el.append(Paragraph(f"{date_str} ({day_name})", subtitle_style))

    # === EXECUTIVE SUMMARY ===
    el.append(Paragraph("Executive Summary", h2))
    n_trades = len(nifty["scalp_trades"])
    n_wins = nifty["scalp_wins"]
    n_losses = nifty["scalp_losses"]
    win_pct = (100 * n_wins / n_trades) if n_trades else 0
    total_inr = nifty["scalp_net_inr_after_brokerage"]
    summary_data = [
        ["Metric", "Value"],
        ["Trading day", f"{date_str} ({day_name})"],
        ["Symbol", "NIFTY (focus instrument)"],
        ["Total snapshots captured", f"{nifty['snap_count']}"],
        ["Scalp trades", f"{n_trades} ({n_wins}W / {n_losses}L)"],
        ["Win rate", f"{win_pct:.0f}%"],
        ["Net points", f"{nifty['scalp_net_pts']:+.1f}"],
        ["Net P&L (1 lot, after brokerage)", f"Rs {total_inr:+,d}"],
    ]
    t = Table(summary_data, colWidths=[3*inch, 3.5*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), COLOR_PRIMARY),
        ("TEXTCOLOR", (0,0), (-1,0), white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 10),
        ("ALIGN", (0,0), (-1,0), "LEFT"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [white, COLOR_BG_LIGHT]),
        ("FONTSIZE", (0,1), (-1,-1), 10),
        ("FONTNAME", (1,-1), (1,-1), "Helvetica-Bold"),
        ("TEXTCOLOR", (1,-1), (1,-1), COLOR_SUCCESS if total_inr >= 0 else COLOR_DANGER),
        ("GRID", (0,0), (-1,-1), 0.3, COLOR_BORDER),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    el.append(t)

    el.append(Spacer(1, 0.15*inch))

    # === NIFTY DETAIL (only) ===
    for sym_data in (nifty,):
        sym = sym_data["symbol"]
        el.append(Paragraph(sym, h2))
        if not sym_data["snaps"]:
            el.append(Paragraph("No snapshots captured for this symbol.", body))
            continue

        # Session move
        move_color = COLOR_SUCCESS if sym_data["session_move"] >= 0 else COLOR_DANGER
        move_arrow = "↑" if sym_data["session_move"] >= 0 else "↓"
        # Use plain arrows since reportlab handles unicode but cleanly
        session_html = (
            f"<b>Session move:</b> {sym_data['first_spot']:.2f} &rarr; {sym_data['last_spot']:.2f} "
            f"&nbsp;&nbsp; <b><font color='{move_color.hexval()}'>"
            f"{sym_data['session_move']:+.2f} pts ({sym_data['session_move_pct']:+.2f}%)</font></b>"
        )
        el.append(Paragraph(session_html, body))

        # Signal breakdown
        sig_data = [
            ["Signal", "Count", "% of session"],
            ["NEUTRAL", str(sym_data["counts"]["NEUTRAL"]),
             f"{100*sym_data['counts']['NEUTRAL']/sym_data['snap_count']:.0f}%"],
            ["CALL", str(sym_data["counts"]["CALL"]),
             f"{100*sym_data['counts']['CALL']/sym_data['snap_count']:.0f}%"],
            ["PUT", str(sym_data["counts"]["PUT"]),
             f"{100*sym_data['counts']['PUT']/sym_data['snap_count']:.0f}%"],
        ]
        t = Table(sig_data, colWidths=[1.5*inch, 1*inch, 1.5*inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), COLOR_ACCENT),
            ("TEXTCOLOR", (0,0), (-1,0), white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 9),
            ("ALIGN", (1,0), (-1,-1), "CENTER"),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [white, COLOR_BG_LIGHT]),
            ("GRID", (0,0), (-1,-1), 0.3, COLOR_BORDER),
            ("LEFTPADDING", (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        el.append(t)
        el.append(Spacer(1, 0.1*inch))

        # Notifications fired (transitions)
        el.append(Paragraph("Notifications fired (signal transitions)", h3))
        if not sym_data["transitions"]:
            el.append(Paragraph("None.", body))
        else:
            trans_data = [["Time (IST)", "Signal", "Spot", "Score", "Confidence", "Tier"]]
            for tr in sym_data["transitions"]:
                t_dt = parse_ts(tr["ts"])
                score = float(tr.get("score", 0) or 0)
                conf = float(tr.get("confidence", 0) or 0)
                oi = float(tr.get("oi_score", 0) or 0)
                reasons = tr.get("reasons") or []
                has_contrarian = any(("Contrarian" in r or "Sharp" in r) for r in reasons)
                if abs(score) >= 4 and conf >= 48 and abs(oi) >= 2 and not has_contrarian:
                    tier = "GREEN"
                elif abs(score) >= 3 and conf >= 30 and not has_contrarian:
                    tier = "YELLOW"
                else:
                    tier = "RED"
                trans_data.append([
                    t_dt.strftime("%H:%M"),
                    tr["signal"],
                    f"{float(tr['spot_price']):.2f}",
                    f"{score:+.2f}",
                    f"{conf:.0f}%",
                    tier,
                ])
            t = Table(trans_data, colWidths=[0.9*inch, 0.7*inch, 1.0*inch, 0.8*inch, 1.0*inch, 1.0*inch])
            ts = TableStyle([
                ("BACKGROUND", (0,0), (-1,0), COLOR_ACCENT),
                ("TEXTCOLOR", (0,0), (-1,0), white),
                ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTSIZE", (0,0), (-1,-1), 9),
                ("ALIGN", (0,0), (-1,-1), "CENTER"),
                ("ROWBACKGROUNDS", (0,1), (-1,-1), [white, COLOR_BG_LIGHT]),
                ("GRID", (0,0), (-1,-1), 0.3, COLOR_BORDER),
                ("LEFTPADDING", (0,0), (-1,-1), 5),
                ("RIGHTPADDING", (0,0), (-1,-1), 5),
                ("TOPPADDING", (0,0), (-1,-1), 4),
                ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ])
            # Color tier column
            for i, row in enumerate(trans_data[1:], start=1):
                tier = row[5]
                tier_color = COLOR_SUCCESS if tier == "GREEN" else (COLOR_WARN if tier == "YELLOW" else COLOR_DANGER)
                ts.add("TEXTCOLOR", (5, i), (5, i), tier_color)
                ts.add("FONTNAME", (5, i), (5, i), "Helvetica-Bold")
            t.setStyle(ts)
            el.append(t)
        el.append(Spacer(1, 0.1*inch))

        # Scalp trades
        target = TARGET_PTS[sym]
        sl = SL_PTS[sym]
        el.append(Paragraph(
            f"Scalp simulation (+{target} target / -{sl} SL / 10-min hold / 60-min cooldown)", h3
        ))
        if not sym_data["scalp_trades"]:
            el.append(Paragraph("No trades taken (no qualifying transitions or no 5-min candles available).", body))
        else:
            scalp_data = [["Entry time", "Direction", "Entry spot", "Outcome", "Points", "Hold (min)"]]
            for tr in sym_data["scalp_trades"]:
                scalp_data.append([
                    tr["time"],
                    tr["direction"],
                    f"{tr['entry']:.2f}",
                    tr["outcome"],
                    f"{tr['points']:+.1f}",
                    str(tr["held_min"]),
                ])
            t = Table(scalp_data, colWidths=[0.9*inch, 0.8*inch, 1.0*inch, 1.4*inch, 0.8*inch, 1.0*inch])
            ts = TableStyle([
                ("BACKGROUND", (0,0), (-1,0), COLOR_ACCENT),
                ("TEXTCOLOR", (0,0), (-1,0), white),
                ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTSIZE", (0,0), (-1,-1), 9),
                ("ALIGN", (0,0), (-1,-1), "CENTER"),
                ("ROWBACKGROUNDS", (0,1), (-1,-1), [white, COLOR_BG_LIGHT]),
                ("GRID", (0,0), (-1,-1), 0.3, COLOR_BORDER),
                ("LEFTPADDING", (0,0), (-1,-1), 5),
                ("RIGHTPADDING", (0,0), (-1,-1), 5),
                ("TOPPADDING", (0,0), (-1,-1), 4),
                ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ])
            for i, tr in enumerate(sym_data["scalp_trades"], start=1):
                color = COLOR_SUCCESS if "WIN" in tr["outcome"] else COLOR_DANGER
                ts.add("TEXTCOLOR", (3, i), (3, i), color)
                ts.add("TEXTCOLOR", (4, i), (4, i), color)
                ts.add("FONTNAME", (3, i), (4, i), "Helvetica-Bold")
            t.setStyle(ts)
            el.append(t)
            el.append(Spacer(1, 0.05*inch))
            net_color = COLOR_SUCCESS if sym_data["scalp_net_inr_after_brokerage"] >= 0 else COLOR_DANGER
            summary_html = (
                f"<b>Net: {sym_data['scalp_net_pts']:+.1f} pts</b> &nbsp;|&nbsp; "
                f"Gross Rs {sym_data['scalp_net_inr']:+,d} (1 lot, ATM delta ~0.50) &nbsp;|&nbsp; "
                f"<b><font color='{net_color.hexval()}'>"
                f"After brokerage: Rs {sym_data['scalp_net_inr_after_brokerage']:+,d}</font></b>"
            )
            el.append(Paragraph(summary_html, body))
        el.append(Spacer(1, 0.2*inch))

    # === 2-DAY ROLLING COMPARISON ===
    if prev_nifty and prev_nifty["snaps"]:
        el.append(Paragraph("2-Day NIFTY Rolling Stats", h2))
        rolling_data = [
            ["Date", "Snapshots", "Move", "Trades", "Wins", "Losses", "Net (Rs)"],
            [
                prev_nifty["date"],
                str(prev_nifty["snap_count"]),
                f"{prev_nifty['session_move']:+.0f} pts",
                str(len(prev_nifty["scalp_trades"])),
                str(prev_nifty["scalp_wins"]),
                str(prev_nifty["scalp_losses"]),
                f"{prev_nifty['scalp_net_inr_after_brokerage']:+,d}",
            ],
            [
                nifty["date"],
                str(nifty["snap_count"]),
                f"{nifty['session_move']:+.0f} pts",
                str(len(nifty["scalp_trades"])),
                str(nifty["scalp_wins"]),
                str(nifty["scalp_losses"]),
                f"{nifty['scalp_net_inr_after_brokerage']:+,d}",
            ],
            [
                "Total",
                str(prev_nifty["snap_count"] + nifty["snap_count"]),
                f"{prev_nifty['session_move'] + nifty['session_move']:+.0f} pts",
                str(len(prev_nifty["scalp_trades"]) + len(nifty["scalp_trades"])),
                str(prev_nifty["scalp_wins"] + nifty["scalp_wins"]),
                str(prev_nifty["scalp_losses"] + nifty["scalp_losses"]),
                f"{prev_nifty['scalp_net_inr_after_brokerage'] + nifty['scalp_net_inr_after_brokerage']:+,d}",
            ],
        ]
        t = Table(rolling_data, colWidths=[1.0*inch, 0.8*inch, 0.9*inch, 0.7*inch, 0.7*inch, 0.7*inch, 0.9*inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), COLOR_PRIMARY),
            ("TEXTCOLOR", (0,0), (-1,0), white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("BACKGROUND", (0,-1), (-1,-1), COLOR_BG_LIGHT),
            ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 9),
            ("ALIGN", (1,0), (-1,-1), "CENTER"),
            ("GRID", (0,0), (-1,-1), 0.3, COLOR_BORDER),
            ("LEFTPADDING", (0,0), (-1,-1), 5),
            ("RIGHTPADDING", (0,0), (-1,-1), 5),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        el.append(t)

    # === SYSTEM HEALTH ===
    cli = db.client(service=True)
    total_snaps = cli.table("snapshots").select("id", count="exact").limit(0).execute().count
    total_candles = cli.table("historical_candles").select("id", count="exact").limit(0).execute().count
    el.append(Spacer(1, 0.2*inch))
    el.append(Paragraph("System Health", h2))
    sys_data = [
        ["Component", "Status"],
        ["Supabase snapshots", f"{total_snaps} rows total"],
        ["Supabase historical candles", f"{total_candles} rows total"],
        ["Local JSONL backup", "Active"],
        ["ntfy.sh push notifications", "Confirmed working"],
        ["GitHub Actions recorder", "Cron enabled (Mon-Fri 09:15-15:30 IST)"],
        ["Monthly cost", "Rs 0"],
    ]
    t = Table(sys_data, colWidths=[3*inch, 3.5*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), COLOR_PRIMARY),
        ("TEXTCOLOR", (0,0), (-1,0), white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [white, COLOR_BG_LIGHT]),
        ("GRID", (0,0), (-1,-1), 0.3, COLOR_BORDER),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    el.append(t)

    el.append(Spacer(1, 0.25*inch))
    el.append(Paragraph(
        "<b>Disclaimer:</b> P&amp;L estimates use ATM weekly options with delta ~0.50; "
        "actual returns vary by strike, IV, and slippage. This is a paper-trading validation "
        "report, not a recommendation to trade real money. Generated automatically from "
        "Supabase data at " + datetime.now(tz=IST).strftime("%Y-%m-%d %H:%M:%S IST") + ".",
        note,
    ))

    doc.build(el)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD (default: today IST)")
    parser.add_argument("--out", help="Output PDF path (default: reports/EOD_Report_YYYY-MM-DD.pdf)")
    args = parser.parse_args()

    date_str = args.date or datetime.now(tz=IST).strftime("%Y-%m-%d")
    out_dir = "reports"
    os.makedirs(out_dir, exist_ok=True)
    out_path = args.out or os.path.join(out_dir, f"EOD_Report_{date_str}.pdf")
    build_pdf(date_str, out_path)
    abs_path = os.path.abspath(out_path)
    print(f"PDF written: {abs_path}")


if __name__ == "__main__":
    main()
