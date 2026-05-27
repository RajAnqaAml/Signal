"""Dry-run today's SENSEX (or any symbol) data through the signal engine.

Replays 5-min candles for a given date, generates signals every 5 minutes,
simulates P&L with the same scalp logic as the dashboard, and prints a
snapshot-by-snapshot report matching the day-view format.

Usage:
    python dry_run_today.py                         # today, SENSEX
    python dry_run_today.py --symbol NIFTY          # today, NIFTY
    python dry_run_today.py --date 2026-05-26       # specific date
    python dry_run_today.py --symbol SENSEX --symbol NIFTY --symbol BANKNIFTY  # all three
"""
import argparse
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from app import (
    generate_signal, compute_rsi, compute_macd, compute_ema,
    compute_supertrend, compute_atr, compute_adx, compute_orb, IST,
)

try:
    import db as _db
except ImportError:
    _db = None


def load_candles_for_date(symbol, date_str, interval=5):
    """Load 5-min candles for a specific date from Supabase."""
    if _db is None or not _db.is_configured():
        print("ERROR: Supabase not configured")
        return []
    rows = _db.get_candles(symbol, start_date=date_str, end_date=date_str, interval=interval)
    out = []
    for r in rows:
        ts_iso = r["ts"]
        if ts_iso.endswith("Z"):
            ts_iso = ts_iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_iso)
        out.append({
            "ts": int(dt.timestamp()),
            "open": float(r["open"]) if r["open"] is not None else None,
            "high": float(r["high"]) if r["high"] is not None else None,
            "low": float(r["low"]) if r["low"] is not None else None,
            "close": float(r["close"]) if r["close"] is not None else None,
            "volume": int(r["volume"]) if r["volume"] is not None else 0,
        })
    out.sort(key=lambda x: x["ts"])
    return out


def load_prev_close(symbol, date_str, interval=5):
    """Get previous day's close from candle data."""
    from datetime import timedelta
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for days_back in range(1, 8):
        prev = (dt - timedelta(days=days_back)).strftime("%Y-%m-%d")
        rows = load_candles_for_date(symbol, prev, interval)
        if rows:
            return rows[-1]["close"]
    return None


NOTIFY_CFG = {
    "NIFTY":     {"lot": 75, "step": 50,  "target": 30,  "sl": 15},
    "BANKNIFTY": {"lot": 15, "step": 100, "target": 60,  "sl": 30},
    "SENSEX":    {"lot": 20, "step": 100, "target": 100, "sl": 50},
}


def run_day(symbol, date_str):
    """Replay one day through the engine. Returns list of snapshot dicts."""
    candles = load_candles_for_date(symbol, date_str)
    if not candles:
        print(f"  No candles for {symbol} on {date_str}")
        return [], []

    prev_close = load_prev_close(symbol, date_str)
    if prev_close is None:
        prev_close = candles[0]["open"]

    cfg = NOTIFY_CFG.get(symbol, NOTIFY_CFG["NIFTY"])
    day_open = candles[0]["open"]
    high_so_far = candles[0]["high"]
    low_so_far = candles[0]["low"]
    closes_so_far = []

    snapshots = []
    trades = []
    active_trade = None
    last_exit_dir = None

    for i, c in enumerate(candles):
        closes_so_far.append(c["close"])
        high_so_far = max(high_so_far, c["high"])
        low_so_far = min(low_so_far, c["low"])

        if i < 6:
            continue

        ts = c["ts"]
        spot = c["close"]
        dt = datetime.fromtimestamp(ts, tz=IST)

        spot_data = {
            "price": spot,
            "change": round((spot - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "open": day_open,
            "high": high_so_far,
            "low": low_so_far,
            "prev_close": prev_close,
        }
        vix_data = {"value": 15, "change": 0}
        breadth = {"advances": 25, "declines": 20, "unchanged": 5}

        closes_arr = np.array(closes_so_far)
        highs_arr = np.array([cc["high"] for cc in candles[:i+1]])
        lows_arr = np.array([cc["low"] for cc in candles[:i+1]])
        atr_val = compute_atr(highs_arr, lows_arr, closes_arr, period=14) if len(closes_arr) >= 15 else None

        adx_di = compute_adx(highs_arr, lows_arr, closes_arr) if len(closes_arr) >= 30 else None
        orb = compute_orb(highs_arr, lows_arr, closes_arr, dt) if len(closes_arr) >= 13 else None
        technicals = {
            "rsi": compute_rsi(closes_arr),
            "macd": compute_macd(closes_arr),
            "ema9": compute_ema(closes_arr, 9),
            "ema21": compute_ema(closes_arr, 21),
            "supertrend": compute_supertrend(highs_arr, lows_arr, closes_arr),
            "atr": atr_val,
            "adx_di": adx_di,
            "orb": orb,
            "history_source": "real",
            "bars": len(closes_arr),
        }

        sig = generate_signal(
            spot_data, vix_data, oi_analysis=None, breadth=breadth,
            technicals=technicals, oc_analysis=None,
            history_source="real", symbol=symbol, now_ist=dt,
        )

        snap = {
            "time": dt.strftime("%H:%M"),
            "spot": spot,
            "signal": sig["signal"],
            "score": sig["score"],
            "confidence": sig["confidence"],
            "tier": sig.get("push_tier", ""),
            "entry": sig["entry"],
            "target1": sig["target1"],
            "target2": sig["target2"],
            "stop_loss": sig["stop_loss"],
            "adx": adx_di["adx"] if adx_di else None,
            "plus_di": adx_di["plus_di"] if adx_di else None,
            "minus_di": adx_di["minus_di"] if adx_di else None,
            "orb": orb.get("orb_signal") if orb else None,
        }
        snapshots.append(snap)

        # --- Scalp trade simulation (same logic as frontend day.js) ---
        # Check if active trade hit target or SL
        if active_trade:
            t = active_trade
            if t["direction"] == "CALL":
                if c["high"] >= t["target1"]:
                    t["exit_time"] = dt.strftime("%H:%M")
                    t["exit_price"] = t["target1"]
                    t["outcome"] = "T1_HIT"
                    t["pts"] = t["target1"] - t["entry"]
                    trades.append(t)
                    last_exit_dir = t["direction"]
                    active_trade = None
                elif c["low"] <= t["stop_loss"]:
                    t["exit_time"] = dt.strftime("%H:%M")
                    t["exit_price"] = t["stop_loss"]
                    t["outcome"] = "SL_HIT"
                    t["pts"] = t["stop_loss"] - t["entry"]
                    trades.append(t)
                    last_exit_dir = t["direction"]
                    active_trade = None
            else:  # PUT
                if c["low"] <= t["target1"]:
                    t["exit_time"] = dt.strftime("%H:%M")
                    t["exit_price"] = t["target1"]
                    t["outcome"] = "T1_HIT"
                    t["pts"] = t["entry"] - t["target1"]
                    trades.append(t)
                    last_exit_dir = t["direction"]
                    active_trade = None
                elif c["high"] >= t["stop_loss"]:
                    t["exit_time"] = dt.strftime("%H:%M")
                    t["exit_price"] = t["stop_loss"]
                    t["outcome"] = "SL_HIT"
                    t["pts"] = t["entry"] - c["high"]
                    trades.append(t)
                    last_exit_dir = t["direction"]
                    active_trade = None

        # Reset last_exit_dir when signal returns to NEUTRAL (allows fresh entry next time)
        if sig["signal"] == "NEUTRAL":
            last_exit_dir = None

        # Open new trade when: signal fires, ADX allows it, no active trade,
        # and not the same direction as a trade we just exited
        gate_blocked = any("G10" in b for b in sig.get("tier_blocks", []))
        can_enter = (sig["signal"] != "NEUTRAL"
                     and not gate_blocked
                     and active_trade is None
                     and sig["signal"] != last_exit_dir)
        if can_enter:
                    active_trade = {
                        "direction": sig["signal"],
                        "entry_time": dt.strftime("%H:%M"),
                        "entry": spot,
                        "target1": sig["target1"],
                        "stop_loss": sig["stop_loss"],
                        "score": sig["score"],
                        "tier": sig.get("push_tier", ""),
                    }

    # Close any open trade at EOD
    if active_trade:
        t = active_trade
        t["exit_time"] = "15:30"
        t["exit_price"] = candles[-1]["close"]
        if t["direction"] == "CALL":
            t["pts"] = t["exit_price"] - t["entry"]
        else:
            t["pts"] = t["entry"] - t["exit_price"]
        t["outcome"] = "EOD_EXIT"
        trades.append(t)

    return snapshots, trades


def print_report(symbol, date_str, snapshots, trades):
    """Print the day report in a format matching the dashboard."""
    cfg = NOTIFY_CFG.get(symbol, NOTIFY_CFG["NIFTY"])
    lot = cfg["lot"]

    print(f"\n{'='*80}")
    print(f"  {symbol} DRY RUN — {date_str}")
    print(f"{'='*80}")

    if not snapshots:
        print("  No data")
        return

    # Snapshot timeline
    print(f"\n  {'TIME':>5s}  {'SPOT':>10s}  {'SIGNAL':>7s}  {'SCORE':>6s}  {'ADX':>5s}  {'+DI':>5s}  {'-DI':>5s}  {'ORB':>7s}  {'TIER':>6s}")
    print(f"  {'-'*66}")

    prev_signal = "NEUTRAL"
    for s in snapshots:
        marker = ""
        if s["signal"] != "NEUTRAL" and s["signal"] != prev_signal:
            marker = " <<< ENTRY"
        elif s["signal"] == "NEUTRAL" and prev_signal != "NEUTRAL":
            marker = " <<< EXIT"

        tier_str = s["tier"] if s["tier"] else ""
        adx_str = f"{s['adx']:5.1f}" if s.get("adx") else "  N/A"
        pdi_str = f"{s['plus_di']:5.1f}" if s.get("plus_di") else "  N/A"
        mdi_str = f"{s['minus_di']:5.1f}" if s.get("minus_di") else "  N/A"
        orb_str = f"{s['orb']:>7s}" if s.get("orb") else "    N/A"
        sig_str = s["signal"] if s["signal"] != "NEUTRAL" else "--"

        print(f"  {s['time']:>5s}  {s['spot']:>10,.2f}  {sig_str:>7s}  {s['score']:>+6.1f}  {adx_str}  {pdi_str}  {mdi_str}  {orb_str}  {tier_str:>6s}{marker}")

        prev_signal = s["signal"]

    # Trade summary
    if trades:
        print(f"\n  {'-'*74}")
        print(f"  TRADES:")
        print(f"  {'#':>2s}  {'DIR':>4s}  {'ENTRY TIME':>10s}  {'ENTRY':>10s}  {'EXIT TIME':>10s}  {'EXIT':>10s}  {'OUTCOME':>8s}  {'PTS':>8s}  {'₹ P&L':>10s}")
        print(f"  {'-'*74}")

        total_pts = 0
        total_inr = 0
        wins = 0
        losses = 0

        for i, t in enumerate(trades):
            pts = t["pts"]
            inr = int(pts * 0.5 * lot)
            total_pts += pts
            total_inr += inr
            if pts > 0:
                wins += 1
            else:
                losses += 1

            print(f"  {i+1:>2d}  {t['direction']:>4s}  {t['entry_time']:>10s}  {t['entry']:>10,.2f}  {t['exit_time']:>10s}  {t['exit_price']:>10,.2f}  {t['outcome']:>8s}  {pts:>+8.1f}  {'+' if inr >= 0 else ''}{inr:>9,d}")

        print(f"  {'-'*74}")
        print(f"  TOTAL: {len(trades)} trades | {wins}W / {losses}L | {total_pts:>+.1f} pts | {'+'if total_inr>=0 else ''}{total_inr:,d} INR (lot={lot}, delta=0.50)")
    else:
        print(f"\n  No trades triggered.")

    # Signal distribution
    signals = [s["signal"] for s in snapshots]
    call_count = signals.count("CALL")
    put_count = signals.count("PUT")
    neutral_count = signals.count("NEUTRAL")
    print(f"\n  Signal Distribution: {call_count} CALL | {put_count} PUT | {neutral_count} NEUTRAL (of {len(signals)} snapshots)")


def main():
    parser = argparse.ArgumentParser(description="Dry-run day replay through signal engine")
    parser.add_argument("--symbol", action="append", default=None,
                        help="Symbol(s) to replay (default: SENSEX NIFTY BANKNIFTY)")
    parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today IST)")
    args = parser.parse_args()

    symbols = args.symbol or ["SENSEX", "NIFTY", "BANKNIFTY"]
    date_str = args.date or datetime.now(tz=IST).strftime("%Y-%m-%d")

    print(f"Signal Engine Dry Run — {date_str}")

    for symbol in symbols:
        snapshots, trades = run_day(symbol, date_str)
        print_report(symbol, date_str, snapshots, trades)

    print()


if __name__ == "__main__":
    main()
