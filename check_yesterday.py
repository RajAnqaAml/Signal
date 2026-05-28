"""Backtest every NIFTY snapshot YESTERDAY (2026-05-21).
Same rules as check_all_36.py: score>=0 -> CALL, score<0 -> PUT, score==0 -> no trade.
Engine rule: +75 pts target, -60 pts SL.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import db

IST = ZoneInfo("Asia/Kolkata")
yesterday = (datetime.now(tz=IST).date() - timedelta(days=1)).strftime("%Y-%m-%d")
rows = db.get_snapshots(yesterday, "NIFTY")

ENGINE_TGT = 75
ENGINE_SL = 60
PT_TO_INR = 0.50 * 75

def fmt_ist(ts_str):
    return datetime.fromisoformat(ts_str).astimezone(IST).strftime("%H:%M")

def walk(entry_spot, future_rows, direction):
    sign = 1 if direction == "CALL" else -1
    last = None
    for r in future_rows:
        sp = r["spot_price"]
        d = (sp - entry_spot) * sign
        hit_t = d >= ENGINE_TGT
        hit_s = d <= -ENGINE_SL
        if hit_t and hit_s:
            return ("both?", r, 0.0)
        if hit_t:
            return ("target", r, ENGINE_TGT * PT_TO_INR)
        if hit_s:
            return ("stop", r, -ENGINE_SL * PT_TO_INR)
        last = r
    if last is None:
        return ("no-data", None, 0.0)
    eod = (last["spot_price"] - entry_spot) * sign
    return ("eod", last, eod * PT_TO_INR)

print(f"NIFTY snapshots {yesterday}: {len(rows)}\n")
print(f"{'#':>2} {'time':<6} {'spot':>9} {'sc':>4} {'tr':>3} {'oi':>3} {'sig':>7} {'conf':>5} {'dir':>4} "
      f"{'best':>7} {'worst':>7} {'outcome':<7} {'hit@':<6} {'P&L Rs':>9}")
print("-" * 110)

totals = {"target": 0, "stop": 0, "eod": 0, "both?": 0, "no-data": 0, "skip": 0}
total_pl = 0.0
trade_count = 0
engine_fired_pl = 0.0
engine_fired_count = 0

for idx, r in enumerate(rows, start=1):
    score = r.get("score") or 0
    trend = r.get("trend_score") or 0
    oi = r.get("oi_score") or 0
    actual_sig = r.get("signal") or "NEUTRAL"
    conf = r.get("confidence") or 0
    spot = r["spot_price"]
    future = rows[idx:]

    if score == 0:
        totals["skip"] += 1
        print(f"{idx:>2} {fmt_ist(r['ts']):<6} {spot:>9.2f} {score:>+4.0f} "
              f"{trend:>+3.0f} {oi:>+3.0f} {actual_sig:>7} {conf:>5.1f} {'-':>4} "
              f"{'-':>7} {'-':>7} {'flat':<7} {'-':<6} {0:>+9.0f}")
        continue

    direction = "CALL" if score > 0 else "PUT"
    sign = 1 if direction == "CALL" else -1
    if future:
        favs = [(rr["spot_price"] - spot) * sign for rr in future]
        best = max(favs); worst = min(favs)
    else:
        best = worst = 0.0

    outcome, hit_row, pl = walk(spot, future, direction)
    totals[outcome] += 1
    if outcome in ("target", "stop", "eod"):
        total_pl += pl
        trade_count += 1
    if actual_sig != "NEUTRAL":
        engine_fired_pl += pl
        engine_fired_count += 1
    hit_time = fmt_ist(hit_row["ts"]) if hit_row else "-"

    print(f"{idx:>2} {fmt_ist(r['ts']):<6} {spot:>9.2f} {score:>+4.0f} "
          f"{trend:>+3.0f} {oi:>+3.0f} {actual_sig:>7} {conf:>5.1f} {direction:>4} "
          f"{best:>+7.1f} {worst:>+7.1f} {outcome:<7} {hit_time:<6} {pl:>+9.0f}")

print("-" * 110)
print(f"\nOutcomes: target={totals['target']}, stop={totals['stop']}, "
      f"eod={totals['eod']}, both?={totals['both?']}, "
      f"flat(score=0)={totals['skip']}")
print(f"\n>> If every signal-leaning snap (|score|>=1) had been entered: "
      f"{trade_count} trades, Net P&L: Rs {total_pl:+,.0f}")
print(f">> Restricting to ENGINE-fired snaps only (signal != NEUTRAL, |score|>=3): "
      f"{engine_fired_count} trades, Net P&L: Rs {engine_fired_pl:+,.0f}")

# Direction split
calls = [r for r in rows if (r.get("score") or 0) > 0]
puts = [r for r in rows if (r.get("score") or 0) < 0]
flats = [r for r in rows if (r.get("score") or 0) == 0]
print(f"\nDirection split: CALL-leaning={len(calls)}, PUT-leaning={len(puts)}, "
      f"flat={len(flats)}")
