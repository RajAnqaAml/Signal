"""For each of the 6 notifications, find when price first crossed
+/-30 pts and +/-45 pts in the signal direction. Walks forward to EOD.

Caveat: snapshot resolution is ~10 min, so 'first time reached' is rounded
up to the next snap. Intra-bar touches are invisible.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import db

IST = ZoneInfo("Asia/Kolkata")

notifications = [
    ("2026-05-20", "12:30", "CALL", "20-May #24"),
    ("2026-05-20", "13:31", "CALL", "20-May #31"),
    ("2026-05-20", "14:12", "CALL", "20-May #35"),
    ("2026-05-21", "11:07", "PUT",  "21-May #4"),
    ("2026-05-21", "11:57", "PUT",  "21-May #9"),
    ("2026-05-21", "12:17", "PUT",  "21-May #11"),
]
THRESHOLDS = [30, 45]


def fmt(ts_iso):
    return datetime.fromisoformat(ts_iso).astimezone(IST)


def find_entry_snap(rows, target_time):
    return min(rows, key=lambda r: abs((fmt(r["ts"]) - target_time).total_seconds()))


def analyze(rows, entry_snap, direction):
    """Return dict of milestones for one notification."""
    entry_ts = fmt(entry_snap["ts"])
    entry_spot = entry_snap["spot_price"]
    sign = 1 if direction == "CALL" else -1
    future = [r for r in rows if fmt(r["ts"]) > entry_ts]

    out = {
        "entry_ts": entry_ts,
        "entry_spot": entry_spot,
        "max_fav": (0.0, None),  # (pts, ts)
        "max_adv": (0.0, None),
    }
    for thr in THRESHOLDS:
        out[f"fav_{thr}"] = None  # first snap delta >= +thr
        out[f"adv_{thr}"] = None  # first snap delta <= -thr

    for r in future:
        delta = (r["spot_price"] - entry_spot) * sign
        ts = fmt(r["ts"])
        if delta > out["max_fav"][0]:
            out["max_fav"] = (delta, ts)
        if delta < out["max_adv"][0]:
            out["max_adv"] = (delta, ts)
        for thr in THRESHOLDS:
            if out[f"fav_{thr}"] is None and delta >= thr:
                out[f"fav_{thr}"] = (ts, r["spot_price"])
            if out[f"adv_{thr}"] is None and delta <= -thr:
                out[f"adv_{thr}"] = (ts, r["spot_price"])
    return out


def mins_after(entry_ts, ts):
    return (ts - entry_ts).total_seconds() / 60


by_date = {}
for d, *_ in notifications:
    by_date.setdefault(d, db.get_snapshots(d, "NIFTY"))

for date_str, hhmm, direction, label in notifications:
    rows = by_date[date_str]
    h, m = map(int, hhmm.split(":"))
    target_t = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=IST)
    entry = find_entry_snap(rows, target_t)
    res = analyze(rows, entry, direction)
    et = res["entry_ts"]

    print(f"\n=== {label}  {direction}  Entry {et.strftime('%H:%M:%S')} IST  Spot {res['entry_spot']:.2f} ===")

    # Favorable milestones
    for thr in THRESHOLDS:
        info = res[f"fav_{thr}"]
        if info:
            ts, spot = info
            print(f"  FAV +{thr} pts reached at {ts.strftime('%H:%M:%S')} IST  "
                  f"(+{mins_after(et, ts):.0f} min)  spot {spot:.2f}")
        else:
            print(f"  FAV +{thr} pts NEVER reached in the day")
    # Adverse milestones
    for thr in THRESHOLDS:
        info = res[f"adv_{thr}"]
        if info:
            ts, spot = info
            print(f"  ADV -{thr} pts reached at {ts.strftime('%H:%M:%S')} IST  "
                  f"(+{mins_after(et, ts):.0f} min)  spot {spot:.2f}")
        else:
            print(f"  ADV -{thr} pts NEVER reached in the day")
    # Max favorable / adverse
    pts, ts = res["max_fav"]
    if ts:
        print(f"  PEAK FAV: {pts:+.1f} pts  at {ts.strftime('%H:%M:%S')} IST  "
              f"(+{mins_after(et, ts):.0f} min)")
    pts, ts = res["max_adv"]
    if ts:
        print(f"  PEAK ADV: {pts:+.1f} pts  at {ts.strftime('%H:%M:%S')} IST  "
              f"(+{mins_after(et, ts):.0f} min)")
