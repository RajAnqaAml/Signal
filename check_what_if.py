"""Backtest: what would have happened if we had bought NIFTY CALL at each
snapshot today where 1 <= score < 3 (mid-tier — below the actual trade threshold).

For each candidate entry, walk forward through the rest of the day's snapshots
and check, in chronological order, whether the engine's CALL target/SL would
have been hit first. Also report the scalp rule (notify.py: +30 pts target,
-15 pts SL, exit in 10 min) and the end-of-day P&L.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import db

IST = ZoneInfo("Asia/Kolkata")
today = datetime.now(tz=IST).date().strftime("%Y-%m-%d")

rows = db.get_snapshots(today, "NIFTY")
print(f"NIFTY snapshots today ({today}): {len(rows)}\n")

# Engine rule (app.py: NIFTY step=50): target1 = +75, target2 = +150, SL = -60
ENGINE_T1 = 75
ENGINE_T2 = 150
ENGINE_SL = -60

# Scalp rule (notify.py)
SCALP_TGT = 30
SCALP_SL = -15

# Approx P&L per ATM CE per lot (ATM_DELTA=0.50, NIFTY lot=75) -> 1 spot pt ≈ Rs 37.5
PT_TO_INR = 0.50 * 75

def fmt_ist(ts_str):
    return datetime.fromisoformat(ts_str).astimezone(IST).strftime("%H:%M:%S IST")

def walk_forward(entry_spot, future_rows, tgt_pts, sl_pts, time_limit_min=None,
                 entry_ts=None):
    """Walk through future snapshots in order; return outcome and the snapshot
    where it triggered. time_limit_min=None means walk to EOD."""
    if entry_ts is not None:
        cutoff = entry_ts + timedelta(minutes=time_limit_min) if time_limit_min else None
    else:
        cutoff = None
    last = None
    for r in future_rows:
        ts = datetime.fromisoformat(r["ts"])
        if cutoff and ts > cutoff:
            return ("time-out", last)
        sp = r["spot_price"]
        delta = sp - entry_spot
        # NOTE: at 10-min resolution we can't tell which side of the bar was
        # touched first; we use the close-only proxy. Both could trigger inside
        # the same bar — flag that ambiguity.
        hit_tgt = delta >= tgt_pts
        hit_sl = delta <= sl_pts
        if hit_tgt and hit_sl:
            return ("both-touched-bar", r)  # ambiguous
        if hit_tgt:
            return ("target", r)
        if hit_sl:
            return ("stop", r)
        last = r
    return ("eod", last)

# Find the 6 candidates
candidates = []
for i, r in enumerate(rows):
    s = r.get("score") or 0
    if 1 <= s < 3:
        candidates.append((i, r))

print(f"Candidates with 1 <= score < 3:  {len(candidates)}")
print("=" * 90)

for idx, (i, r) in enumerate(candidates, start=1):
    entry_ts = datetime.fromisoformat(r["ts"])
    entry_spot = r["spot_price"]
    score = r["score"]
    trend = r["trend_score"]
    oi = r["oi_score"]
    future = rows[i+1:]

    print(f"\n[{idx}] Entry @ {fmt_ist(r['ts'])}   spot={entry_spot:.2f}   "
          f"score={score:+.1f} (trend={trend:+.0f}, oi={oi:+.0f})")
    if not future:
        print("    (last snapshot of the day — no forward data)")
        continue

    # Engine rule (full-day window)
    outcome_eng, hit_row_eng = walk_forward(entry_spot, future, ENGINE_T1, ENGINE_SL)
    if hit_row_eng:
        hit_spot = hit_row_eng["spot_price"]
        hit_delta = hit_spot - entry_spot
        hit_ts = fmt_ist(hit_row_eng["ts"])
        print(f"    Engine rule (+75 / -60): {outcome_eng:18s} "
              f"@ {hit_ts}  spot={hit_spot:.2f}  Δ={hit_delta:+.2f} pts  "
              f"Rs={hit_delta * PT_TO_INR:+.0f}")
    else:
        print(f"    Engine rule (+75 / -60): {outcome_eng}")

    # Scalp rule — 10-min exit window
    outcome_sc, hit_row_sc = walk_forward(
        entry_spot, future, SCALP_TGT, SCALP_SL,
        time_limit_min=10, entry_ts=entry_ts)
    if hit_row_sc:
        hit_spot = hit_row_sc["spot_price"]
        hit_delta = hit_spot - entry_spot
        hit_ts = fmt_ist(hit_row_sc["ts"])
        print(f"    Scalp rule (+30/-15, 10m): {outcome_sc:16s} "
              f"@ {hit_ts}  spot={hit_spot:.2f}  Δ={hit_delta:+.2f} pts  "
              f"Rs={hit_delta * PT_TO_INR:+.0f}")
    else:
        print(f"    Scalp rule (+30/-15, 10m): {outcome_sc}")

    # Max favorable / max adverse across the rest of the day (close-only)
    deltas = [(rr["spot_price"] - entry_spot, rr["ts"]) for rr in future]
    max_fav = max(deltas, key=lambda x: x[0])
    max_adv = min(deltas, key=lambda x: x[0])
    eod_delta = future[-1]["spot_price"] - entry_spot
    print(f"    Max favorable: {max_fav[0]:+.2f} pts  @ {fmt_ist(max_fav[1])}")
    print(f"    Max adverse:   {max_adv[0]:+.2f} pts  @ {fmt_ist(max_adv[1])}")
    print(f"    EOD close:     {eod_delta:+.2f} pts  "
          f"(Rs {eod_delta * PT_TO_INR:+.0f} per lot, held to last snap)")

# Summary
print("\n" + "=" * 90)
print("Summary table (engine rule, full-day hold):")
print(f"{'#':<3}{'entry IST':<12}{'spot':<10}{'score':<8}{'outcome':<20}{'P&L Rs':<10}")
n_target = n_stop = n_eod = 0
total_pl = 0.0
for idx, (i, r) in enumerate(candidates, start=1):
    entry_spot = r["spot_price"]
    future = rows[i+1:]
    outcome, hit_row = walk_forward(entry_spot, future, ENGINE_T1, ENGINE_SL)
    if outcome == "target":
        pl = ENGINE_T1 * PT_TO_INR
        n_target += 1
    elif outcome == "stop":
        pl = ENGINE_SL * PT_TO_INR
        n_stop += 1
    else:  # eod close
        pl = (future[-1]["spot_price"] - entry_spot) * PT_TO_INR if future else 0
        n_eod += 1
    total_pl += pl
    print(f"{idx:<3}{fmt_ist(r['ts'])[:8]:<12}{entry_spot:<10.2f}{r['score']:+.1f}   "
          f"{outcome:<20}{pl:+.0f}")
print(f"\nWins / Losses / EOD-exits: {n_target} / {n_stop} / {n_eod}")
print(f"Total P&L if we'd taken all 6 (1 lot each): Rs {total_pl:+.0f}")
