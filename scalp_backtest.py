"""Scalp backtest with FIXED ±20 pt exits and 10-min time cap.

Strategy:
  - Take the FIRST non-NEUTRAL signal of each session (or after 60-min cooldown)
  - Enter at the signal candle's close
  - Walk forward up to MAX_CANDLES (default 2 = 10 min)
  - WIN  = +20 pts in signal's direction reached intra-candle
  - LOSS = -20 pts against signal reached intra-candle
  - Conservative tie-break: if both happen in same candle, assume SL hit first
  - If neither hits by MAX_CANDLES, exit at last candle's CLOSE

Usage:
    python scalp_backtest.py --symbol NIFTY --target 20 --sl 20 --max-candles 2
    python scalp_backtest.py --symbol BANKNIFTY --target 50 --sl 50 --max-candles 2
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from app import generate_signal, compute_rsi, compute_macd, compute_ema, compute_supertrend, IST
from backtest_history import load_candles, load_vix, group_by_day, find_nearest_vix


def simulate_trade(entry_idx, candles_today, direction, target, sl, max_candles):
    """Walk forward from entry. Return (outcome, points, candles_held).
    outcome ∈ {'WIN', 'LOSS', 'TIMEOUT_WIN', 'TIMEOUT_LOSS', 'TIMEOUT_FLAT'}
    """
    entry_price = candles_today[entry_idx]["close"]
    future = candles_today[entry_idx + 1 : entry_idx + 1 + max_candles]
    if not future:
        return "NO_DATA", 0, 0

    for j, fc in enumerate(future):
        hi, lo = fc["high"], fc["low"]
        if direction == "CALL":
            # win if high - entry >= target; loss if entry - low >= sl
            hit_win = (hi - entry_price) >= target
            hit_loss = (entry_price - lo) >= sl
        else:  # PUT
            hit_win = (entry_price - lo) >= target
            hit_loss = (hi - entry_price) >= sl

        if hit_win and hit_loss:
            # Both in same candle — conservative: assume SL hit first
            return "LOSS", -sl, j + 1
        if hit_loss:
            return "LOSS", -sl, j + 1
        if hit_win:
            return "WIN", target, j + 1

    # Time exit: take whatever's at the last candle's close
    exit_price = future[-1]["close"]
    if direction == "CALL":
        pts = exit_price - entry_price
    else:
        pts = entry_price - exit_price
    if pts > 0:
        return "TIMEOUT_WIN", round(pts, 2), len(future)
    elif pts < 0:
        return "TIMEOUT_LOSS", round(pts, 2), len(future)
    else:
        return "TIMEOUT_FLAT", 0, len(future)


def run_day(date_str, candles_today, prev_close, vix_map, args):
    """For each candle, compute signal. If non-NEUTRAL and no recent trade, simulate."""
    trades = []
    if len(candles_today) < 10:
        return trades

    day_open = candles_today[0]["open"]
    high_so_far = candles_today[0]["high"]
    low_so_far = candles_today[0]["low"]
    closes_so_far = []
    last_trade_idx = -1000  # cooldown anchor

    for i, c in enumerate(candles_today):
        closes_so_far.append(c["close"])
        high_so_far = max(high_so_far, c["high"])
        low_so_far = min(low_so_far, c["low"])
        if i < 6:
            continue
        if i + args.max_candles >= len(candles_today):
            break
        ts = c["ts"]
        dt = datetime.fromtimestamp(ts, tz=IST)
        # only act 09:30-14:30 IST (avoid morning chaos + closing volatility)
        if (dt.hour, dt.minute) < (9, 30) or (dt.hour, dt.minute) > (14, 30):
            continue
        # Cooldown after last trade
        if (i - last_trade_idx) * 5 < args.cooldown_min:
            continue

        spot = c["close"]
        change_pct = ((spot - prev_close) / prev_close * 100) if prev_close else 0
        spot_data = {
            "price": spot, "change": round(change_pct, 2),
            "open": day_open, "high": high_so_far, "low": low_so_far,
            "prev_close": prev_close,
        }
        vix_data = {"value": round(find_nearest_vix(vix_map, ts), 2), "change": 0}
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
        sig = generate_signal(
            spot_data, vix_data, oi_analysis=None,
            breadth={"advances": 0, "declines": 0, "unchanged": 0},
            technicals=technicals, oc_analysis=None,
            history_source="real", symbol=args.symbol, now_ist=dt,
        )
        if sig["signal"] == "NEUTRAL":
            continue

        outcome, points, held = simulate_trade(
            i, candles_today, sig["signal"], args.target, args.sl, args.max_candles
        )
        trades.append({
            "date": date_str, "time": dt.strftime("%H:%M"),
            "signal": sig["signal"], "score": sig["score"],
            "confidence": sig["confidence"],
            "entry": spot,
            "outcome": outcome, "points": points, "held_candles": held,
        })
        last_trade_idx = i
    return trades


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="NIFTY", choices=["NIFTY", "BANKNIFTY"])
    p.add_argument("--target", type=float, default=20, help="Target points in signal direction")
    p.add_argument("--sl", type=float, default=20, help="Stop-loss points against signal")
    p.add_argument("--max-candles", type=int, default=2, help="Max 5-min candles to hold (2=10min)")
    p.add_argument("--cooldown-min", type=int, default=60, help="Min minutes since last trade in same symbol")
    args = p.parse_args()

    candles = load_candles(args.symbol)
    vix_map = load_vix()
    by_day = group_by_day(candles)
    days = sorted(by_day.keys())
    print(f"{args.symbol}: {len(candles)} candles across {len(days)} days")
    print(f"Rules: +{args.target} / -{args.sl} / max {args.max_candles * 5}min hold / {args.cooldown_min}min cooldown")
    print()

    all_trades = []
    prev_close = None
    for day in days:
        ct = by_day[day]
        if prev_close is None:
            prev_close = ct[0]["open"]
        trades = run_day(day, ct, prev_close, vix_map, args)
        all_trades.extend(trades)
        prev_close = ct[-1]["close"]

    if not all_trades:
        print("No trades fired.")
        return

    # Per-day summary
    by_day_trades = defaultdict(list)
    for t in all_trades:
        by_day_trades[t["date"]].append(t)
    print(f"{'date':>10s}  {'trades':>6s}  {'wins':>4s}  {'loss':>4s}  {'net_pts':>8s}  details")
    print("-" * 80)
    cum_pts = 0
    for d in sorted(by_day_trades.keys()):
        ts = by_day_trades[d]
        wins = sum(1 for t in ts if t["outcome"] in ("WIN", "TIMEOUT_WIN"))
        losses = sum(1 for t in ts if t["outcome"] in ("LOSS", "TIMEOUT_LOSS"))
        pts = sum(t["points"] for t in ts)
        cum_pts += pts
        detail = " ".join(f"{t['time']}{t['signal'][0]}{t['outcome'][:3]}{t['points']:+.0f}" for t in ts)
        print(f"{d:>10s}  {len(ts):>6d}  {wins:>4d}  {losses:>4d}  {pts:>+8.2f}  {detail[:90]}")

    print()
    n = len(all_trades)
    wins = sum(1 for t in all_trades if t["outcome"] in ("WIN", "TIMEOUT_WIN"))
    losses = sum(1 for t in all_trades if t["outcome"] in ("LOSS", "TIMEOUT_LOSS"))
    flats = sum(1 for t in all_trades if t["outcome"] == "TIMEOUT_FLAT")
    win_rate = 100 * wins / n
    total_pts = sum(t["points"] for t in all_trades)
    avg_per_trade = total_pts / n
    avg_per_day = total_pts / len(by_day_trades)
    breakeven_winrate = args.sl / (args.target + args.sl) * 100

    print(f"=== AGGREGATE ({args.symbol}) ===")
    print(f"Days with trades:   {len(by_day_trades)} / {len(days)}")
    print(f"Total trades:       {n}")
    print(f"Wins:               {wins}  ({win_rate:.1f}%)")
    print(f"Losses:             {losses}  ({100*losses/n:.1f}%)")
    print(f"Flat (timeout 0):   {flats}  ({100*flats/n:.1f}%)")
    print(f"Net points:         {total_pts:+.2f}")
    print(f"Per-trade avg:      {avg_per_trade:+.2f}")
    print(f"Per-day avg:        {avg_per_day:+.2f}")
    print(f"Breakeven win rate: {breakeven_winrate:.1f}% (before costs)")
    edge = win_rate - breakeven_winrate
    print(f"Edge vs breakeven:  {edge:+.1f} pts")
    print()
    print("Outcome breakdown:")
    for outcome, count in Counter(t["outcome"] for t in all_trades).most_common():
        print(f"  {outcome:>14s}: {count:>4d} ({100*count/n:.1f}%)")


if __name__ == "__main__":
    main()
