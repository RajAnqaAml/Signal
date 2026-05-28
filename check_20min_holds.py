"""Re-run the 6 NIFTY notifications across 2026-05-20 and 2026-05-21
with a max 20-min hold (NOT EOD).

Exit rules (whichever fires first):
  - Target hit  : +75 pts in direction -> +Rs 2,812 per lot
  - Stop hit    : -60 pts against -> -Rs 2,250 per lot
  - Time exit   : at the snap closest to entry+20min, P&L = realised pts * Rs 37.50
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db

IST = ZoneInfo("Asia/Kolkata")
ENGINE_TGT = 75
ENGINE_SL = 60
PT_TO_INR = 0.50 * 75  # Rs 37.50 per spot point per lot
MAX_HOLD_MIN = 20

# The 6 notifications the user flagged (from the Excel report)
notifications = [
    # (date, entry_time_str, expected_dir, label)
    ("2026-05-20", "12:30", "CALL", "20-May #24"),
    ("2026-05-20", "13:31", "CALL", "20-May #31"),
    ("2026-05-20", "14:12", "CALL", "20-May #35"),
    ("2026-05-21", "11:07", "PUT",  "21-May #4"),
    ("2026-05-21", "11:57", "PUT",  "21-May #9"),
    ("2026-05-21", "12:17", "PUT",  "21-May #11"),
]


def fmt(ts_iso):
    return datetime.fromisoformat(ts_iso).astimezone(IST)


def find_entry_snap(rows, target_time):
    """Closest snap (by absolute time) to the target HH:MM."""
    return min(rows, key=lambda r: abs((fmt(r["ts"]) - target_time).total_seconds()))


def evaluate(rows, entry_snap, direction):
    entry_ts = fmt(entry_snap["ts"])
    entry_spot = entry_snap["spot_price"]
    sign = 1 if direction == "CALL" else -1
    cutoff = entry_ts + timedelta(minutes=MAX_HOLD_MIN)

    # Walk forward snaps within the hold window
    future = [r for r in rows if fmt(r["ts"]) > entry_ts]
    in_window = [r for r in future if fmt(r["ts"]) <= cutoff]

    if not in_window:
        return ("no-forward", None, None, 0.0)

    # Bar-by-bar check (10-min cadence; we only see close prices, so this is
    # a close-only proxy)
    for r in in_window:
        delta = (r["spot_price"] - entry_spot) * sign
        if delta >= ENGINE_TGT and delta <= -ENGINE_SL:
            return ("both?", r, delta, 0.0)  # shouldn't happen at 10-min resolution
        if delta >= ENGINE_TGT:
            return ("target", r, delta, ENGINE_TGT * PT_TO_INR)
        if delta <= -ENGINE_SL:
            return ("stop", r, delta, -ENGINE_SL * PT_TO_INR)

    # Time exit at the last in-window snap
    exit_snap = in_window[-1]
    exit_delta = (exit_snap["spot_price"] - entry_spot) * sign
    return ("20-min-exit", exit_snap, exit_delta, exit_delta * PT_TO_INR)


# Group queries by date so we only fetch each day once
by_date = {}
for d, *_ in notifications:
    by_date.setdefault(d, db.get_snapshots(d, "NIFTY"))


print(f"{'Label':<14} {'Entry IST':<10} {'Spot':>9} {'Dir':>4} "
      f"{'Exit IST':<10} {'Exit Spot':>9} {'Pts':>6} {'Outcome':<13} {'P&L Rs':>8}")
print("-" * 100)

total = 0.0
target_hits = stop_hits = time_exits = 0
for date_str, entry_hhmm, direction, label in notifications:
    rows = by_date[date_str]
    h, m = map(int, entry_hhmm.split(":"))
    target_t = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=IST)
    entry = find_entry_snap(rows, target_t)
    entry_ts = fmt(entry["ts"])
    entry_spot = entry["spot_price"]

    outcome, exit_snap, exit_delta, pl = evaluate(rows, entry, direction)
    total += pl
    if outcome == "target":  target_hits += 1
    elif outcome == "stop":  stop_hits += 1
    else:                    time_exits += 1

    exit_str = fmt(exit_snap["ts"]).strftime("%H:%M") if exit_snap else "-"
    exit_spot_str = f"{exit_snap['spot_price']:.2f}" if exit_snap else "-"
    delta_str = f"{exit_delta:+.1f}" if exit_delta is not None else "-"

    print(f"{label:<14} {entry_ts.strftime('%H:%M:%S'):<10} {entry_spot:>9.2f} "
          f"{direction:>4} {exit_str:<10} {exit_spot_str:>9} {delta_str:>6} "
          f"{outcome:<13} {pl:>+8.0f}")

print("-" * 100)
print(f"Target hits : {target_hits}")
print(f"Stop hits   : {stop_hits}")
print(f"Time exits  : {time_exits}")
print(f"Net P&L (1 lot ATM each, 20-min hold): Rs {total:+,.0f}")
