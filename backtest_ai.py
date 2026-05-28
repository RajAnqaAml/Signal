"""backtest_ai.py — Compare signal engine outcomes WITH vs WITHOUT AI filter.

For each TIER_1 entry signal in historical candle data:
  - WITHOUT AI: take every signal -> track P&L
  - WITH AI:    run Gemini filter -> CONFIRM = take, SKIP = skip -> track P&L

AI results are cached in backtest_ai_cache.json to avoid repeated API calls.

Usage:
    python backtest_ai.py --days 7
    python backtest_ai.py --days 7 --no-cache   (re-call Gemini for all signals)

Note: each new signal costs 2 Gemini API calls (~1-2 seconds). With a paid key
this is well within rate limits.
"""
import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime

import numpy as np

from app import (
    generate_signal, compute_rsi, compute_macd, compute_ema,
    compute_supertrend, compute_atr, compute_adx, IST,
)
import db as _db
import notify

DELTA      = 0.5     # ATM delta approximation
MAX_BARS   = 24      # 2-hour max hold (24 × 5-min bars)
CACHE_FILE = "backtest_ai_cache.json"


# ─── Cache helpers ───────────────────────────────────────────────────────────

def _load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _cache_key(symbol, direction, score, dt: datetime):
    return f"{symbol}|{direction}|{score}|{dt.strftime('%Y-%m-%d %H:%M')}"


# ─── Candle loader ────────────────────────────────────────────────────────────

def load_candles(symbol):
    rows = _db.get_candles(symbol, interval=5)
    out = []
    for r in rows:
        ts_iso = r["ts"]
        if ts_iso.endswith("Z"):
            ts_iso = ts_iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_iso)
        out.append({
            "ts":    int(dt.timestamp()),
            "open":  float(r["open"])  if r["open"]  is not None else None,
            "high":  float(r["high"])  if r["high"]  is not None else None,
            "low":   float(r["low"])   if r["low"]   is not None else None,
            "close": float(r["close"]) if r["close"] is not None else None,
        })
    out.sort(key=lambda x: x["ts"])
    return out


def group_by_day(candles):
    by_day = defaultdict(list)
    for c in candles:
        if c["close"] is None:
            continue
        dt = datetime.fromtimestamp(c["ts"], tz=IST)
        by_day[dt.strftime("%Y-%m-%d")].append(c)
    for d in by_day:
        by_day[d].sort(key=lambda x: x["ts"])
    return dict(by_day)


# ─── Trade outcome ────────────────────────────────────────────────────────────

def resolve_trade(trade, candles_today, entry_idx):
    """Walk forward from entry_idx to find T1 / SL / time-stop outcome."""
    direction = trade["direction"]
    t1  = trade["target1"]
    sl  = trade["stop_loss"]

    for j in range(entry_idx + 1, min(entry_idx + 1 + MAX_BARS, len(candles_today))):
        c   = candles_today[j]
        dt  = datetime.fromtimestamp(c["ts"], tz=IST)
        bar = j - entry_idx

        if direction == "CALL":
            if c["high"] >= t1:
                return "T1",  t1,  t1 - trade["entry"],  dt.strftime("%H:%M"), bar
            if c["low"]  <= sl:
                return "SL",  sl,  sl - trade["entry"],  dt.strftime("%H:%M"), bar
        else:
            if c["low"]  <= t1:
                return "T1",  t1,  trade["entry"] - t1,  dt.strftime("%H:%M"), bar
            if c["high"] >= sl:
                return "SL",  sl,  trade["entry"] - sl,  dt.strftime("%H:%M"), bar

    # Time-stop
    last  = candles_today[min(entry_idx + MAX_BARS, len(candles_today) - 1)]
    exit_ = last["close"]
    dt    = datetime.fromtimestamp(last["ts"], tz=IST)
    pts   = (exit_ - trade["entry"]) if direction == "CALL" else (trade["entry"] - exit_)
    return "2HR", exit_, pts, dt.strftime("%H:%M"), MAX_BARS


# ─── Per-day signal replay ────────────────────────────────────────────────────

def run_day(candles, prev_close, symbol, cache, use_cache, no_cache):
    """Replay one day through engine. Returns list of trade dicts (with AI verdict)."""
    if len(candles) < 20:
        return []

    day_open   = candles[0]["open"]
    high_so_far = candles[0]["high"]
    low_so_far  = candles[0]["low"]
    closes_so_far = []
    trades = []
    active = None
    last_exit_dir = None

    for i, c in enumerate(candles):
        closes_so_far.append(c["close"])
        high_so_far = max(high_so_far, c["high"])
        low_so_far  = min(low_so_far,  c["low"])
        if i < 6:
            continue

        ts   = c["ts"]
        spot = c["close"]
        dt   = datetime.fromtimestamp(ts, tz=IST)

        change_pct = round((spot - prev_close) / prev_close * 100, 2) if prev_close else 0
        spot_data  = {
            "price": spot, "change": change_pct, "open": day_open,
            "high": high_so_far, "low": low_so_far, "prev_close": prev_close,
        }

        closes_arr = np.array(closes_so_far)
        highs_arr  = np.array([cc["high"] for cc in candles[:i + 1]])
        lows_arr   = np.array([cc["low"]  for cc in candles[:i + 1]])

        atr_val  = compute_atr(highs_arr, lows_arr, closes_arr, period=14) if len(closes_arr) >= 15 else None
        adx_di   = compute_adx(highs_arr, lows_arr, closes_arr) if len(closes_arr) >= 30 else None

        technicals = {
            "rsi":        compute_rsi(closes_arr),
            "macd":       compute_macd(closes_arr),
            "ema9":       compute_ema(closes_arr, 9),
            "ema21":      compute_ema(closes_arr, 21),
            "supertrend": compute_supertrend(highs_arr, lows_arr, closes_arr),
            "atr":        atr_val,
            "adx_di":     adx_di,
            "history_source": "real",
            "bars": len(closes_arr),
        }

        sig = generate_signal(
            spot_data,
            {"value": 15, "change": 0},
            None,
            {"advances": 25, "declines": 20, "unchanged": 5},
            technicals,
            symbol=symbol,
            now_ist=dt,
            history_source="real",
        )

        # Manage active trade
        if active:
            outcome, exit_px, pts, exit_time, bars = resolve_trade(active, candles, active["entry_idx"])
            if outcome in ("T1", "SL") or bars >= MAX_BARS:
                active["outcome"]    = outcome
                active["exit"]       = exit_px
                active["exit_time"]  = exit_time
                active["pts"]        = pts
                active["bars_held"]  = bars
                trades.append(active)
                last_exit_dir = active["direction"]
                active = None
            # If not resolved yet, keep waiting (checked via `if active` next loop)
            # Note: for simplicity we resolve at next check, not bar-by-bar here.
            # Actual resolution is deferred to end-of-signal or next signal fire.

        if sig["signal"] == "NEUTRAL":
            last_exit_dir = None
            # Flush pending active trade on NEUTRAL if time exceeded
            if active:
                outcome, exit_px, pts, exit_time, bars = resolve_trade(
                    active, candles, active["entry_idx"]
                )
                active["outcome"]   = outcome
                active["exit"]      = exit_px
                active["exit_time"] = exit_time
                active["pts"]       = pts
                active["bars_held"] = bars
                trades.append(active)
                last_exit_dir = active["direction"]
                active = None
            continue

        if active is not None or sig["signal"] == last_exit_dir:
            continue

        tier_blocks = sig.get("tier_blocks", [])
        push_tier   = sig.get("push_tier", "TIER_3")

        # Only simulate TIER_1 signals
        if push_tier != "TIER_1":
            continue

        # G10 critical block — skip
        if any("G10" in b for b in tier_blocks):
            continue

        # ── Run AI filter ─────────────────────────────────────────────────
        ai_verdict = "CONFIRM"   # default if AI disabled
        ai_reason  = "AI not run"
        ai_risk    = "N/A"

        ck = _cache_key(symbol, sig["signal"], sig["score"], dt)

        if not no_cache and ck in cache:
            cached = cache[ck]
            ai_verdict = cached["verdict"]
            ai_reason  = cached["reason"]
            ai_risk    = cached.get("risk", "MEDIUM")
            print(f"  [AI cache] {dt.strftime('%H:%M')} {sig['signal']} -> {ai_verdict} ({ai_reason[:50]})")
        else:
            # Build a minimal context for backtesting (no live option chain)
            from market_context import _adx_from_tier_blocks, _dte, _EXPIRY_NAME
            mock_ctx = {
                "exchange":        "BSE" if symbol == "SENSEX" else "NSE",
                "lot_size":        notify.LOT_SIZE.get(symbol, 75),
                "strike_gap":      notify.STRIKE_STEP.get(symbol, 50),
                "expiry_day":      _EXPIRY_NAME.get(symbol, "Tuesday"),
                "dte":             _dte(symbol, dt),
                "atm_premium":     int(atr_val * 0.55 * max(_dte(symbol, dt), 0.5) ** 0.5) if atr_val else 110,
                "iv":              "~15",
                "vix":             15.0,
                "rsi":             round(float(technicals["rsi"] or 50), 1),
                "adx":             _adx_from_tier_blocks(tier_blocks),
                "score_100":       max(0, min(100, int((float(sig["score"]) + 10) * 5))),
                "us_market":       "Unavailable (backtest mode)",
                "sgx_change":      "N/A (backtest mode)",
                "capital_at_risk": notify.LOT_SIZE.get(symbol, 75) * 110,
            }

            from ai_filter import evaluate_signal
            ai_res = evaluate_signal(symbol, sig, mock_ctx, now_ist=dt)
            ai_verdict = ai_res["verdict"]
            ai_reason  = ai_res["reason"]
            ai_risk    = ai_res["risk"]

            # Save to cache
            cache[ck] = {"verdict": ai_verdict, "reason": ai_reason, "risk": ai_risk}
            if not use_cache:
                _save_cache(cache)   # save after each call so progress isn't lost
            time.sleep(0.5)  # gentle rate limiting

        active = {
            "direction":  sig["signal"],
            "entry_time": dt.strftime("%H:%M"),
            "entry_idx":  i,
            "entry":      spot,
            "target1":    sig["target1"],
            "stop_loss":  sig["stop_loss"],
            "score":      sig["score"],
            "push_tier":  push_tier,
            "ai_verdict": ai_verdict,
            "ai_reason":  ai_reason,
            "ai_risk":    ai_risk,
        }

    # EOD close
    if active:
        last = candles[-1]
        exit_ = last["close"]
        dt_last = datetime.fromtimestamp(last["ts"], tz=IST)
        pts = (exit_ - active["entry"]) if active["direction"] == "CALL" else (active["entry"] - exit_)
        active.update({
            "outcome": "EOD", "exit": exit_,
            "exit_time": dt_last.strftime("%H:%M"),
            "pts": pts, "bars_held": MAX_BARS,
        })
        trades.append(active)

    return trades


# ─── Print helpers ────────────────────────────────────────────────────────────

_VERDICT_ICON = {"CONFIRM": "OK", "CAUTION": "~~", "SKIP": "XX"}


def print_results(symbol, all_trades, lot):
    print(f"\n{'='*90}")
    print(f"  {symbol}  (lot={lot})")
    print(f"{'='*90}")
    print(f"{'DATE':>10}  {'TIME':>5}  {'DIR':>5}  {'AI':>7}  {'OUT':>6}  {'PTS':>7}  {'INR':>9}  REASON")
    print("-" * 90)

    total_raw_pts = total_raw_inr = raw_wins = raw_losses = 0
    total_ai_pts  = total_ai_inr  = ai_wins  = ai_losses  = ai_skipped = 0
    prev_date = None

    for t in all_trades:
        entry_dt = datetime.strptime(t["date"] + " " + t["entry_time"], "%Y-%m-%d %H:%M")
        date_show = t["date"] if t["date"] != prev_date else " " * 10
        prev_date = t["date"]

        inr_raw  = int(t["pts"] * DELTA * lot)
        outcome  = t.get("outcome", "?")
        verdict  = t.get("ai_verdict", "CONFIRM")
        icon     = _VERDICT_ICON.get(verdict, "??")
        dir_s    = t["direction"][0]
        reason   = (t.get("ai_reason") or "")[:35]

        # Raw stats (no AI)
        total_raw_pts  += t["pts"]
        total_raw_inr  += inr_raw
        if t["pts"] > 0: raw_wins += 1
        else:             raw_losses += 1

        # AI-filtered stats
        if verdict == "SKIP":
            ai_skipped += 1
            inr_ai   = 0
            pts_ai   = 0.0
            out_show = "SKIP"
        else:
            total_ai_pts += t["pts"]
            total_ai_inr += inr_raw
            pts_ai = t["pts"]
            if t["pts"] > 0: ai_wins += 1
            else:             ai_losses += 1
            out_show = f"{dir_s} {outcome}"

        print(f"{date_show:>10}  {t['entry_time']:>5}  {t['direction']:>5}  {icon:>7}  "
              f"{out_show:>6}  {pts_ai:>+7.1f}  {inr_raw if verdict!='SKIP' else 0:>+9,d}  {reason}")

    total_raw_trades = raw_wins + raw_losses
    total_ai_trades  = ai_wins + ai_losses

    print("-" * 90)
    print(f"\n  WITHOUT AI filter:  {total_raw_trades} trades  "
          f"{raw_wins}W/{raw_losses}L  "
          f"WR={100*raw_wins/total_raw_trades:.0f}%  "
          f"P&L: Rs{total_raw_inr:+,d}")
    print(f"  WITH    AI filter:  {total_ai_trades} trades  "
          f"{ai_wins}W/{ai_losses}L  "
          f"WR={100*ai_wins/total_ai_trades:.0f}% (if any)  "
          f"P&L: Rs{total_ai_inr:+,d}  "
          f"({ai_skipped} signals skipped by AI)")

    return {
        "raw":  (total_raw_inr, total_raw_trades, raw_wins),
        "ai":   (total_ai_inr,  total_ai_trades,  ai_wins, ai_skipped),
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",     type=int, default=7,   help="Number of trading days")
    parser.add_argument("--symbols",  default="NIFTY,BANKNIFTY", help="Comma-separated symbols")
    parser.add_argument("--no-cache", action="store_true",   help="Ignore cache, re-call Gemini for all")
    args = parser.parse_args()

    symbols  = [s.strip() for s in args.symbols.split(",")]
    cache    = {} if args.no_cache else _load_cache()

    print(f"\n{'='*90}")
    print(f"  AI FILTER BACKTEST — last {args.days} days, symbols: {', '.join(symbols)}")
    print(f"  Mode: {'fresh (no cache)' if args.no_cache else 'cached'}")
    print(f"{'='*90}")

    grand_raw_inr = grand_raw_trades = grand_raw_wins = 0
    grand_ai_inr  = grand_ai_trades  = grand_ai_wins  = grand_ai_skipped = 0

    for symbol in symbols:
        print(f"\nLoading candles for {symbol}...")
        candles = load_candles(symbol)
        by_day  = group_by_day(candles)
        all_days = sorted(by_day.keys())
        days     = all_days[-args.days:]
        lot      = notify.LOT_SIZE.get(symbol, 75)

        prev_close = None
        if all_days.index(days[0]) > 0:
            prev_day   = all_days[all_days.index(days[0]) - 1]
            prev_close = by_day[prev_day][-1]["close"]

        all_trades = []
        for d in days:
            day_candles = by_day[d]
            print(f"  {d} ({len(day_candles)} candles)...")
            trades = run_day(day_candles, prev_close, symbol, cache, not args.no_cache, args.no_cache)
            for t in trades:
                t["date"] = d
            all_trades.extend(trades)
            prev_close = day_candles[-1]["close"]

        if all_trades:
            res = print_results(symbol, all_trades, lot)
            grand_raw_inr   += res["raw"][0]; grand_raw_trades += res["raw"][1]; grand_raw_wins += res["raw"][2]
            grand_ai_inr    += res["ai"][0];  grand_ai_trades  += res["ai"][1];  grand_ai_wins  += res["ai"][2]
            grand_ai_skipped += res["ai"][3]

    if not args.no_cache:
        _save_cache(cache)

    print(f"\n{'='*90}")
    print(f"  GRAND TOTAL ({len(symbols)} symbols, {args.days} days)")
    print(f"  WITHOUT AI: {grand_raw_trades} trades  "
          f"{grand_raw_wins}W/{grand_raw_trades-grand_raw_wins}L  "
          f"WR={100*grand_raw_wins/grand_raw_trades:.0f}%  "
          f"P&L: Rs{grand_raw_inr:+,d}" if grand_raw_trades else "  No trades")
    print(f"  WITH    AI: {grand_ai_trades} trades  "
          f"{grand_ai_wins}W/{grand_ai_trades-grand_ai_wins}L  "
          f"WR={100*grand_ai_wins/grand_ai_trades:.0f}%  "
          f"P&L: Rs{grand_ai_inr:+,d}  "
          f"({grand_ai_skipped} skipped)" if grand_ai_trades else "  No trades after filter")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
