"""6-month V3 backtest on Yahoo OHLC candles (flow-blind, factors 1-4 and 7 only).

CAVEAT: This is FLOW-BLIND. Factors 5 (OI Spurts), 6 (breadth), 8 (option
chain) are NOT available historically -- they're live-only. So this backtest
runs with ~5 of 8 factors. Results should be interpreted as:
  - "Did the V3 GATES filter improve P&L on price-action-only signals?"
  - NOT as "What will the live engine do?"

The live engine has 60-100% more signal weight (OI/breadth/option-chain) so
the live numbers will differ from this backtest. Use this to validate the
V3 GATE LOGIC, not the absolute P&L numbers.

For each historical 5-min candle in the last 6 months:
  - Compute spot_data + technicals from the candle history
  - Run generate_signal() (this gives current-engine output AND push_tier)
  - Tier 1 fires = "auto-push" simulation (would have pushed under V3)
  - All current-engine fires = baseline for comparison
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np

from app import (IST, generate_signal, compute_rsi, compute_macd, compute_ema,
                  compute_supertrend, compute_atr)
from backtest_history import load_candles, load_vix, group_by_day, find_nearest_vix

PT_TO_INR = {"NIFTY": 0.5 * 75, "BANKNIFTY": 0.5 * 15}
HORIZON_BARS = 6  # 6 x 5min = 30 min forward window for trade simulation


def run_day(date_str, candles_today, prev_close, vix_map, symbol):
    """Walk each 5-min candle of the day. Return list of (kind, fire_dict)
    where kind in {"current", "tier1", "tier2_watch"}."""
    out = []
    if len(candles_today) < 12:
        return out

    day_open = candles_today[0]["open"]
    high_so_far = candles_today[0]["high"]
    low_so_far = candles_today[0]["low"]
    closes_so_far = []
    prev_sig = ""

    for i, c in enumerate(candles_today):
        closes_so_far.append(c["close"])
        high_so_far = max(high_so_far, c["high"])
        low_so_far = min(low_so_far, c["low"])
        if i < 6:
            continue
        if i + HORIZON_BARS >= len(candles_today):
            break

        ts = c["ts"]
        dt = datetime.fromtimestamp(ts, tz=IST)
        if dt.hour < 9 or (dt.hour == 9 and dt.minute < 30):
            continue
        if (dt.hour, dt.minute) >= (15, 30):
            continue

        spot = c["close"]
        change_pct = ((spot - prev_close) / prev_close * 100) if prev_close else 0
        spot_data = {
            "price": spot,
            "change": round(change_pct, 2),
            "open": day_open,
            "high": high_so_far,
            "low": low_so_far,
            "prev_close": prev_close,
        }
        vix_val = find_nearest_vix(vix_map, ts)
        vix_data = {"value": round(vix_val, 2), "change": 0}

        closes_arr = np.array(closes_so_far)
        highs_arr = np.array([cc["high"] for cc in candles_today[:i+1]])
        lows_arr = np.array([cc["low"] for cc in candles_today[:i+1]])
        atr_val = compute_atr(highs_arr, lows_arr, closes_arr, period=14) if len(closes_arr) >= 15 else None
        technicals = {
            "rsi": compute_rsi(closes_arr),
            "macd": compute_macd(closes_arr),
            "ema9": compute_ema(closes_arr, 9),
            "ema21": compute_ema(closes_arr, 21),
            "supertrend": compute_supertrend(highs_arr, lows_arr, closes_arr),
            "atr": atr_val,
            "history_source": "real",
            "bars": len(closes_arr),
        }

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

        direction = sig["signal"]
        if direction == "NEUTRAL":
            prev_sig = direction
            continue

        # Only count transitions (NEUTRAL/opposite -> CALL/PUT)
        if prev_sig == direction:
            continue
        prev_sig = direction

        # Simulate forward trade outcome with engine's own targets
        future = candles_today[i+1 : i+1+HORIZON_BARS]
        pnl_pts = simulate_trade(future, sig, spot)
        pnl_inr = pnl_pts * PT_TO_INR[symbol]

        push_tier = sig.get("push_tier", "TIER_3")
        fire = {
            "date": date_str,
            "time": dt.strftime("%H:%M"),
            "direction": direction,
            "spot": spot,
            "score": sig["score"],
            "conf": sig["confidence"],
            "push_tier": push_tier,
            "tier_blocks": sig.get("tier_blocks", []),
            "pnl_pts": pnl_pts,
            "pnl_inr": pnl_inr,
        }
        out.append(fire)
    return out


def simulate_trade(future_candles, sig, entry_spot):
    """Walk forward, find first target/SL hit or take exit at last close."""
    direction = sig["signal"]
    target1 = sig["target1"]
    sl = sig["stop_loss"]
    if not future_candles:
        return 0.0
    for fc in future_candles:
        if direction == "CALL":
            if fc["high"] >= target1: return float(target1 - entry_spot)
            if fc["low"] <= sl: return float(sl - entry_spot)
        else:
            if fc["low"] <= target1: return float(entry_spot - target1)
            if fc["high"] >= sl: return float(entry_spot - sl)
    # No target/stop hit -- exit at last close
    last_close = future_candles[-1]["close"]
    return float(last_close - entry_spot) if direction == "CALL" else float(entry_spot - last_close)


def main():
    print("Loading 6-month candle data...", flush=True)
    vix_map = load_vix()

    summary = {}
    for symbol in ("NIFTY", "BANKNIFTY"):
        print(f"\n>>> {symbol} <<<")
        candles = load_candles(symbol)
        by_day = group_by_day(candles)
        days = sorted(by_day.keys())
        print(f"  {len(candles)} candles, {len(days)} days ({days[0]} -> {days[-1]})")

        all_fires = []
        prev_close = None
        for d in days:
            ct = by_day[d]
            if prev_close is None:
                prev_close = ct[0]["open"]
            fires = run_day(d, ct, prev_close, vix_map, symbol)
            all_fires.extend(fires)
            prev_close = ct[-1]["close"]

        # Tally
        current_fires = all_fires  # current engine fires every non-NEUTRAL transition
        tier1_fires = [f for f in all_fires if f["push_tier"] == "TIER_1"]
        tier2_fires = [f for f in all_fires if f["push_tier"] == "TIER_2"]
        tier3_fires = [f for f in all_fires if f["push_tier"] == "TIER_3"]

        cur_pnl = sum(f["pnl_inr"] for f in current_fires)
        cur_wins = sum(1 for f in current_fires if f["pnl_inr"] > 0)
        cur_losses = sum(1 for f in current_fires if f["pnl_inr"] < 0)

        t1_pnl = sum(f["pnl_inr"] for f in tier1_fires)
        t1_wins = sum(1 for f in tier1_fires if f["pnl_inr"] > 0)
        t1_losses = sum(1 for f in tier1_fires if f["pnl_inr"] < 0)

        # Tier 2 results (the most informative in flow-blind mode -- these are
        # signals that pass G4/G7/G8 but fail G1 because OI=0)
        t2_pnl = sum(f["pnl_inr"] for f in tier2_fires)
        t2_wins = sum(1 for f in tier2_fires if f["pnl_inr"] > 0)
        t2_losses = sum(1 for f in tier2_fires if f["pnl_inr"] < 0)

        # Refused (Tier 3) -- what would V3 have AVOIDED?
        t3_pnl = sum(f["pnl_inr"] for f in tier3_fires)
        t3_wins = sum(1 for f in tier3_fires if f["pnl_inr"] > 0)
        t3_losses = sum(1 for f in tier3_fires if f["pnl_inr"] < 0)

        summary[symbol] = {
            "current_count": len(current_fires),
            "current_pnl": cur_pnl,
            "current_winrate": cur_wins / max(1, len(current_fires)),
            "tier1_count": len(tier1_fires),
            "tier1_pnl": t1_pnl,
            "tier1_winrate": t1_wins / max(1, len(tier1_fires)),
            "tier2_count": len(tier2_fires),
            "tier3_refused": len(tier3_fires),
        }

        print(f"\n  CURRENT engine (every non-NEUTRAL transition):")
        print(f"    Fires: {len(current_fires)}")
        print(f"    Wins / Losses: {cur_wins} / {cur_losses}")
        print(f"    Win rate: {summary[symbol]['current_winrate']*100:.1f}%")
        print(f"    Net P&L: Rs {cur_pnl:+,.0f}")
        print(f"\n  V3 TIER 1 (would have pushed to phone):")
        print(f"    Fires: {len(tier1_fires)}  ({len(tier1_fires)/max(1,len(current_fires))*100:.1f}% of current)")
        print(f"    Wins / Losses: {t1_wins} / {t1_losses}")
        print(f"    Win rate: {summary[symbol]['tier1_winrate']*100:.1f}%")
        print(f"    Net P&L: Rs {t1_pnl:+,.0f}")
        print(f"\n  V3 TIER 2 (dashboard watch, no push):")
        print(f"    Fires: {len(tier2_fires)}")
        print(f"    Wins / Losses: {t2_wins} / {t2_losses}")
        print(f"    Win rate: {t2_wins/max(1,len(tier2_fires))*100:.1f}%")
        print(f"    Net P&L: Rs {t2_pnl:+,.0f}  (in flow-blind mode; would be TIER_1 with real OI)")
        print(f"\n  V3 TIER 3 (REFUSED entirely):")
        print(f"    Fires: {len(tier3_fires)}")
        print(f"    Wins / Losses: {t3_wins} / {t3_losses}")
        print(f"    Win rate: {t3_wins/max(1,len(tier3_fires))*100:.1f}%")
        print(f"    Net P&L if traded: Rs {t3_pnl:+,.0f}  (what V3 AVOIDED)")

        # Top blocks (why current fires were refused)
        block_counts = defaultdict(int)
        for f in all_fires:
            if f["push_tier"] != "TIER_1":
                for b in f["tier_blocks"]:
                    gate = b.split(":")[0]
                    block_counts[gate] += 1
        print(f"\n  Why current fires were filtered out (top gates):")
        for gate, cnt in sorted(block_counts.items(), key=lambda x: -x[1])[:6]:
            print(f"    {gate}: {cnt} fires")

    # Grand total
    print("\n" + "=" * 80)
    print("GRAND TOTAL — both symbols, 6 months of Yahoo OHLC (flow-blind)")
    print("=" * 80)
    total_cur = sum(s["current_pnl"] for s in summary.values())
    total_t1 = sum(s["tier1_pnl"] for s in summary.values())
    total_cur_count = sum(s["current_count"] for s in summary.values())
    total_t1_count = sum(s["tier1_count"] for s in summary.values())
    print(f"Current engine: {total_cur_count} fires, Net Rs {total_cur:+,.0f}")
    print(f"V3 Tier 1 only: {total_t1_count} fires, Net Rs {total_t1:+,.0f}")
    print(f"Reduction in trades: {(1 - total_t1_count/max(1,total_cur_count))*100:.1f}%")
    print(f"P&L change:          Rs {total_t1 - total_cur:+,.0f}  "
          f"({(total_t1 - total_cur)/abs(total_cur)*100 if total_cur else 0:+.1f}%)")
    print()
    print("FLOW-BLIND CAVEAT: This is OHLC-only (5 of 8 factors). The live engine")
    print("includes OI Spurts, breadth, and option chain. Actual live numbers will differ.")


if __name__ == "__main__":
    main()
