"""Backtest the engine across 30 days of historical 5-min data from Yahoo.
Uses ONLY the factors we have historical data for: price action (F1-F3),
VIX (F4), and indicators on real history (F7).
Skipped: OI Spurts (F5), breadth (F6), option chain (F8) — no historical source.

Usage: python backtest_history.py --symbol NIFTY --horizon 6
  --horizon = number of 5-min candles to look forward (6 = 30 min)
"""
import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from app import generate_signal, compute_rsi, compute_macd, compute_ema, compute_supertrend, IST

try:
    import db as _db
except ImportError:
    _db = None


def _candles_from_db(symbol):
    """Fetch all 5-min candles for a symbol from Supabase historical_candles.
    Returns the same shape as the JSON file: list of {ts, open, high, low, close, volume}.
    `ts` is unix epoch seconds (to match the Yahoo JSON shape).
    """
    rows = _db.get_candles(symbol, interval=5)
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
    return out


def load_candles(symbol):
    """Load 5-min candles for a symbol. Supabase if configured + populated; else JSON file."""
    if _db is not None and _db.is_configured():
        rows = _candles_from_db(symbol)
        if rows:
            print(f"(candles source: Supabase, {len(rows)} rows for {symbol})")
            return rows
    # Fallback to local JSON file
    path = f"history/{symbol}_5m_30d.json"
    if not os.path.exists(path):
        raise SystemExit(f"No candles for {symbol}. Tried Supabase and {path}.")
    with open(path, encoding="utf-8") as f:
        candles = json.load(f)["candles"]
    print(f"(candles source: JSON file {path}, {len(candles)} rows)")
    return candles


def load_vix():
    """Load VIX 5-min data, return dict keyed by unix timestamp."""
    if _db is not None and _db.is_configured():
        rows = _candles_from_db("VIX")
        if rows:
            out = {r["ts"]: r["close"] for r in rows if r["close"] is not None}
            if out:
                return out
    # Fallback to local JSON file
    path = "history/VIX_5m_30d.json"
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        j = json.load(f)
    res = j["chart"]["result"][0]
    ts = res.get("timestamp", [])
    quote = res["indicators"]["quote"][0]
    closes = quote.get("close", [])
    out = {}
    for i, t in enumerate(ts):
        if closes[i] is not None:
            out[t] = closes[i]
    return out


def group_by_day(candles):
    """Return dict: date_str -> list of candles sorted by ts."""
    by_day = defaultdict(list)
    for c in candles:
        dt = datetime.fromtimestamp(c["ts"], tz=IST)
        date_str = dt.strftime("%Y-%m-%d")
        by_day[date_str].append(c)
    for d in by_day:
        by_day[d].sort(key=lambda c: c["ts"])
    return dict(by_day)


def find_nearest_vix(vix_map, ts):
    """Find closest VIX value to given unix timestamp."""
    if not vix_map:
        return 15.0
    # binary search would be faster; linear is fine for this scale
    keys = sorted(vix_map.keys())
    # find first key >= ts
    lo, hi = 0, len(keys) - 1
    best = keys[0]
    best_dist = abs(keys[0] - ts)
    while lo <= hi:
        mid = (lo + hi) // 2
        d = abs(keys[mid] - ts)
        if d < best_dist:
            best_dist = d
            best = keys[mid]
        if keys[mid] < ts:
            lo = mid + 1
        else:
            hi = mid - 1
    return vix_map[best]


def run_day_backtest(date_str, candles_today, prev_close, vix_map, horizon, symbol):
    """Walk through one day's 5-min candles. At each candle from 09:45 onwards,
    compute signal. If CALL/PUT, simulate trade and look horizon candles forward.
    Returns list of trade dicts.
    """
    trades = []
    if len(candles_today) < 10:
        return trades

    day_open = candles_today[0]["open"]
    high_so_far = candles_today[0]["high"]
    low_so_far = candles_today[0]["low"]

    # Build close array for indicator computation (we'll extend as we walk)
    closes_so_far = []

    # We need at least N candles for indicators (30+)
    # Process candles starting from idx where we have enough history AND at least 30 min into session
    for i, c in enumerate(candles_today):
        closes_so_far.append(c["close"])
        high_so_far = max(high_so_far, c["high"])
        low_so_far = min(low_so_far, c["low"])

        # Skip first 6 candles (30 min warmup) to have data for indicators + intraday context
        if i < 6:
            continue
        # Skip last `horizon` candles (no forward data)
        if i + horizon >= len(candles_today):
            break

        ts = c["ts"]
        spot = c["close"]
        dt = datetime.fromtimestamp(ts, tz=IST)
        # Only act between 09:30 and 14:30
        if dt.hour < 9 or (dt.hour == 9 and dt.minute < 30):
            continue
        if dt.hour > 14 or (dt.hour == 14 and dt.minute > 30):
            continue

        # Build spot_data
        change_pct = ((spot - prev_close) / prev_close * 100) if prev_close else 0
        spot_data = {
            "price": spot,
            "change": round(change_pct, 2),
            "open": day_open,
            "high": high_so_far,
            "low": low_so_far,
            "prev_close": prev_close,
        }

        # VIX
        vix_val = find_nearest_vix(vix_map, ts)
        # We don't have VIX change history easily, set to 0
        vix_data = {"value": round(vix_val, 2), "change": 0}

        # Indicators on real 5-min history
        closes_arr = np.array(closes_so_far)
        highs_arr = np.array([cc["high"] for cc in candles_today[:i+1]])
        lows_arr = np.array([cc["low"] for cc in candles_today[:i+1]])
        technicals = {
            "rsi": compute_rsi(closes_arr),
            "macd": compute_macd(closes_arr),
            "ema9": compute_ema(closes_arr, 9),
            "ema21": compute_ema(closes_arr, 21),
            "supertrend": compute_supertrend(highs_arr, lows_arr, closes_arr),
            "history_source": "real",
            "bars": len(closes_arr),
        }

        # Run engine (no OI, no breadth, no option chain)
        sig = generate_signal(
            spot_data, vix_data,
            oi_analysis=None,
            breadth={"advances": 0, "declines": 0, "unchanged": 0},
            technicals=technicals,
            oc_analysis=None,
            history_source="real",
            symbol=symbol,
            now_ist=dt,
        )

        if sig["signal"] == "NEUTRAL":
            continue

        # Simulate trade outcome
        future_candles = candles_today[i+1 : i+1+horizon]
        outcome = "OPEN"
        exit_idx = horizon - 1  # default to horizon-end
        for j, fc in enumerate(future_candles):
            if sig["signal"] == "CALL":
                if fc["high"] >= sig["target2"]:
                    outcome = "T2_HIT"; exit_idx = j; break
                if fc["high"] >= sig["target1"]:
                    outcome = "T1_HIT"; exit_idx = j; break
                if fc["low"] <= sig["stop_loss"]:
                    outcome = "SL_HIT"; exit_idx = j; break
            elif sig["signal"] == "PUT":
                if fc["low"] <= sig["target2"]:
                    outcome = "T2_HIT"; exit_idx = j; break
                if fc["low"] <= sig["target1"]:
                    outcome = "T1_HIT"; exit_idx = j; break
                if fc["high"] >= sig["stop_loss"]:
                    outcome = "SL_HIT"; exit_idx = j; break

        exit_candle = future_candles[exit_idx] if exit_idx < len(future_candles) else future_candles[-1]
        exit_price = exit_candle["close"]

        if outcome == "OPEN":
            # Use exit_price for verdict
            if sig["signal"] == "CALL":
                outcome = "POSITIVE" if exit_price > spot else "NEGATIVE"
            else:
                outcome = "POSITIVE" if exit_price < spot else "NEGATIVE"

        # Points captured / risked
        if sig["signal"] == "CALL":
            if outcome == "T1_HIT": pts = sig["target1"] - spot
            elif outcome == "T2_HIT": pts = sig["target2"] - spot
            elif outcome == "SL_HIT": pts = sig["stop_loss"] - spot
            else: pts = exit_price - spot
        else:  # PUT
            if outcome == "T1_HIT": pts = spot - sig["target1"]
            elif outcome == "T2_HIT": pts = spot - sig["target2"]
            elif outcome == "SL_HIT": pts = spot - sig["stop_loss"]
            else: pts = spot - exit_price

        trades.append({
            "date": date_str,
            "time": dt.strftime("%H:%M"),
            "signal": sig["signal"],
            "confidence": sig["confidence"],
            "score": sig["score"],
            "entry": spot,
            "exit": exit_price,
            "outcome": outcome,
            "points": round(pts, 2),
            "exit_minutes": (exit_idx + 1) * 5,
        })
    return trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="NIFTY", choices=["NIFTY", "BANKNIFTY", "SENSEX"])
    parser.add_argument("--horizon", type=int, default=6, help="5-min candles to look forward (6=30min)")
    args = parser.parse_args()

    candles = load_candles(args.symbol)
    vix_map = load_vix()
    by_day = group_by_day(candles)
    days = sorted(by_day.keys())
    print(f"Loaded {args.symbol}: {len(candles)} candles across {len(days)} days ({days[0]} -> {days[-1]})")
    print(f"Horizon: {args.horizon * 5} minutes")
    print()

    all_trades = []
    prev_close = None
    for date_str in days:
        candles_today = by_day[date_str]
        if prev_close is None:
            prev_close = candles_today[0]["open"]  # bootstrap
        trades = run_day_backtest(date_str, candles_today, prev_close, vix_map, args.horizon, args.symbol)
        all_trades.extend(trades)
        # Update prev_close for next day
        prev_close = candles_today[-1]["close"]

    if not all_trades:
        print("No signals fired in any session.")
        return

    # Per-day summary
    by_day_trades = defaultdict(list)
    for t in all_trades:
        by_day_trades[t["date"]].append(t)

    print(f"{'date':>10s}  {'trades':>6s}  {'CALL':>4s}  {'PUT':>4s}  {'T1+':>4s}  {'SL':>4s}  {'net_pts':>8s}")
    print("-" * 60)
    total_pts = 0
    total_t1 = 0
    total_sl = 0
    for d in sorted(by_day_trades.keys()):
        ts = by_day_trades[d]
        n = len(ts)
        n_call = sum(1 for t in ts if t["signal"] == "CALL")
        n_put = sum(1 for t in ts if t["signal"] == "PUT")
        n_t1 = sum(1 for t in ts if t["outcome"] in ("T1_HIT", "T2_HIT"))
        n_sl = sum(1 for t in ts if t["outcome"] == "SL_HIT")
        pts = sum(t["points"] for t in ts)
        total_pts += pts
        total_t1 += n_t1
        total_sl += n_sl
        print(f"{d:>10s}  {n:>6d}  {n_call:>4d}  {n_put:>4d}  {n_t1:>4d}  {n_sl:>4d}  {pts:>+8.2f}")

    n_total = len(all_trades)
    print()
    print(f"=== Aggregate ({args.symbol}, {args.horizon*5}-min horizon) ===")
    print(f"Total signals: {n_total}")
    print(f"T1 or T2 hit: {total_t1} ({100*total_t1/n_total:.1f}%)")
    print(f"SL hit:       {total_sl} ({100*total_sl/n_total:.1f}%)")
    print(f"Net points:   {total_pts:+.2f}")
    print(f"Avg per trade: {total_pts/n_total:+.2f} pts")

    # Group by outcome
    from collections import Counter
    outcomes = Counter(t["outcome"] for t in all_trades)
    print()
    print("Outcome breakdown:")
    for outcome, count in outcomes.most_common():
        print(f"  {outcome:>10s}: {count:>4d} ({100*count/n_total:.1f}%)")


if __name__ == "__main__":
    main()
