"""Re-run the 6 NIFTY notifications with 5-min, 10-min, and 20-min holds.

IMPORTANT RESOLUTION CAVEAT:
    Snapshots are recorded every ~10 min (median gap 10.1 min in 2026-05-21
    data). So at a 5-min cap we'll usually have NO forward snap to evaluate
    against. At a 10-min cap we'll have 0 or 1. Results at short caps are
    therefore close-only proxies at best -- intra-bar moves are invisible.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db

IST = ZoneInfo("Asia/Kolkata")
ENGINE_TGT = 75
ENGINE_SL = 60
PT_TO_INR = 0.50 * 75
HOLD_WINDOWS_MIN = [5, 10, 20]

notifications = [
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
    return min(rows, key=lambda r: abs((fmt(r["ts"]) - target_time).total_seconds()))


def evaluate(rows, entry_snap, direction, max_hold_min):
    entry_ts = fmt(entry_snap["ts"])
    entry_spot = entry_snap["spot_price"]
    sign = 1 if direction == "CALL" else -1
    cutoff = entry_ts + timedelta(minutes=max_hold_min)
    in_window = [r for r in rows if entry_ts < fmt(r["ts"]) <= cutoff]
    if not in_window:
        return ("no-forward", None, None, 0.0)
    for r in in_window:
        delta = (r["spot_price"] - entry_spot) * sign
        if delta >= ENGINE_TGT:
            return ("target", r, delta, ENGINE_TGT * PT_TO_INR)
        if delta <= -ENGINE_SL:
            return ("stop", r, delta, -ENGINE_SL * PT_TO_INR)
    exit_snap = in_window[-1]
    exit_delta = (exit_snap["spot_price"] - entry_spot) * sign
    return (f"{max_hold_min}m-exit", exit_snap, exit_delta, exit_delta * PT_TO_INR)


# Cache day-level snapshot pulls
by_date = {}
for d, *_ in notifications:
    by_date.setdefault(d, db.get_snapshots(d, "NIFTY"))

for hold_min in HOLD_WINDOWS_MIN:
    print(f"\n=== Hold cap = {hold_min} min ===")
    print(f"{'Label':<14} {'Entry':<10} {'Spot':>9} {'Dir':>4} "
          f"{'Exit':<10} {'Exit Spot':>9} {'Pts':>6} {'Outcome':<14} {'P&L Rs':>8}")
    print("-" * 96)
    total = 0.0
    no_data = wins = losses = 0
    for date_str, entry_hhmm, direction, label in notifications:
        rows = by_date[date_str]
        h, m = map(int, entry_hhmm.split(":"))
        target_t = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=IST)
        entry = find_entry_snap(rows, target_t)
        entry_ts_str = fmt(entry["ts"]).strftime("%H:%M:%S")
        entry_spot = entry["spot_price"]
        outcome, exit_snap, delta, pl = evaluate(rows, entry, direction, hold_min)
        total += pl
        if outcome == "no-forward":
            no_data += 1
        elif pl > 0:
            wins += 1
        elif pl < 0:
            losses += 1
        exit_str = fmt(exit_snap["ts"]).strftime("%H:%M") if exit_snap else "-"
        exit_spot_str = f"{exit_snap['spot_price']:.2f}" if exit_snap else "-"
        delta_str = f"{delta:+.1f}" if delta is not None else "-"
        print(f"{label:<14} {entry_ts_str:<10} {entry_spot:>9.2f} {direction:>4} "
              f"{exit_str:<10} {exit_spot_str:>9} {delta_str:>6} "
              f"{outcome:<14} {pl:>+8.0f}")
    print("-" * 96)
    print(f"Wins: {wins} | Losses: {losses} | No-forward-data: {no_data} | "
          f"Net P&L: Rs {total:+,.0f}")
