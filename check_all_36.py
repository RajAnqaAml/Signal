"""Backtest every NIFTY snapshot today.
Hypothetical direction follows score sign:  score>=0 -> buy CALL, score<0 -> buy PUT.
Score exactly 0: skip (truly directionless).

For each entry, walk forward in chronological order at 10-min resolution and
apply the engine's rule: target = +75 spot pts, SL = -60 spot pts (direction
adjusted). Also report max favorable / max adverse / EOD P&L.

Resolution caveat: snapshots are ~10 min apart, so when both target and SL
sit inside the same forward bar, we can't tell which was tagged first.
The 'both?' label flags that ambiguity.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import db

IST = ZoneInfo("Asia/Kolkata")
today = datetime.now(tz=IST).date().strftime("%Y-%m-%d")

rows = db.get_snapshots(today, "NIFTY")

ENGINE_TGT = 75   # spot points
ENGINE_SL = 60    # spot points (absolute; sign applied per direction)
PT_TO_INR = 0.50 * 75  # ATM delta * NIFTY lot = Rs 37.5 per spot point

def fmt_ist(ts_str):
    return datetime.fromisoformat(ts_str).astimezone(IST).strftime("%H:%M")

def walk(entry_spot, future_rows, direction):
    """direction = 'CALL' or 'PUT'. Returns (outcome, hit_row, pnl_inr).
    outcome in {target, stop, both?, eod}."""
    sign = 1 if direction == "CALL" else -1
    last = None
    for r in future_rows:
        sp = r["spot_price"]
        delta_dir = (sp - entry_spot) * sign  # favorable if positive
        hit_tgt = delta_dir >= ENGINE_TGT
        hit_sl = delta_dir <= -ENGINE_SL
        if hit_tgt and hit_sl:
            return ("both?", r, 0.0)
        if hit_tgt:
            return ("target", r, ENGINE_TGT * PT_TO_INR)
        if hit_sl:
            return ("stop", r, -ENGINE_SL * PT_TO_INR)
        last = r
    if last is None:
        return ("no-data", None, 0.0)
    eod_delta = (last["spot_price"] - entry_spot) * sign
    return ("eod", last, eod_delta * PT_TO_INR)

print(f"NIFTY snapshots today ({today}): {len(rows)}\n")
print(f"{'#':>2} {'time':<6} {'spot':>9} {'sc':>4} {'tr':>3} {'oi':>3} {'dir':>4} "
      f"{'best':>7} {'worst':>7} {'outcome':<7} {'hit@':<6} {'P&L Rs':>9}")
print("-" * 92)

totals = {"target": 0, "stop": 0, "eod": 0, "both?": 0, "no-data": 0, "skip": 0}
total_pl = 0.0
trade_count = 0

for idx, r in enumerate(rows, start=1):
    score = r.get("score") or 0
    trend = r.get("trend_score") or 0
    oi = r.get("oi_score") or 0
    spot = r["spot_price"]
    future = rows[idx:]  # rows after this index

    if score == 0:
        totals["skip"] += 1
        print(f"{idx:>2} {fmt_ist(r['ts']):<6} {spot:>9.2f} {score:>+4.0f} "
              f"{trend:>+3.0f} {oi:>+3.0f} {'-':>4} "
              f"{'-':>7} {'-':>7} {'flat':<7} {'-':<6} {0:>+9.0f}")
        continue

    direction = "CALL" if score > 0 else "PUT"
    sign = 1 if direction == "CALL" else -1

    # Max favorable / adverse in chosen direction (close-only)
    if future:
        favs = [(rr["spot_price"] - spot) * sign for rr in future]
        best = max(favs)
        worst = min(favs)
    else:
        best = worst = 0.0

    outcome, hit_row, pl = walk(spot, future, direction)
    totals[outcome] += 1
    if outcome in ("target", "stop", "eod"):
        total_pl += pl
        trade_count += 1
    hit_time = fmt_ist(hit_row["ts"]) if hit_row else "-"

    print(f"{idx:>2} {fmt_ist(r['ts']):<6} {spot:>9.2f} {score:>+4.0f} "
          f"{trend:>+3.0f} {oi:>+3.0f} {direction:>4} "
          f"{best:>+7.1f} {worst:>+7.1f} {outcome:<7} {hit_time:<6} {pl:>+9.0f}")

print("-" * 92)
print(f"\nOutcomes: target={totals['target']}, stop={totals['stop']}, "
      f"eod-exit={totals['eod']}, both-touched-in-bar={totals['both?']}, "
      f"skipped(score=0)={totals['skip']}")
print(f"Trades simulated: {trade_count}  (skipping the {totals['skip']} flat ones)")
print(f"Net P&L if every signal-leaning snap had been entered (1 lot ATM): "
      f"Rs {total_pl:+,.0f}")

# Direction breakdown
calls = [r for r in rows if (r.get("score") or 0) > 0]
puts = [r for r in rows if (r.get("score") or 0) < 0]
flats = [r for r in rows if (r.get("score") or 0) == 0]
print(f"\nDirection split: CALL-leaning={len(calls)}, PUT-leaning={len(puts)}, "
      f"flat={len(flats)}")
