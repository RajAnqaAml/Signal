"""Show today's NIFTY score range + the row with max |score|."""
from datetime import datetime
from zoneinfo import ZoneInfo

import db

IST = ZoneInfo("Asia/Kolkata")
today = datetime.now(tz=IST).date().strftime("%Y-%m-%d")

rows = db.get_snapshots(today, "NIFTY")
print(f"NIFTY snapshots today ({today}): {len(rows)}")
if not rows:
    raise SystemExit(0)

scores = [(r["ts"], r.get("score") or 0, r.get("trend_score"), r.get("oi_score"), r.get("signal"), r.get("confidence")) for r in rows]
scores_sorted = sorted(scores, key=lambda x: x[1])

print(f"\nmin score: {scores_sorted[0][1]:+.2f}  at {scores_sorted[0][0]}  (trend={scores_sorted[0][2]}, oi={scores_sorted[0][3]})")
print(f"max score: {scores_sorted[-1][1]:+.2f}  at {scores_sorted[-1][0]}  (trend={scores_sorted[-1][2]}, oi={scores_sorted[-1][3]})")

# row with max abs(score)
abs_sorted = sorted(scores, key=lambda x: abs(x[1]), reverse=True)
print(f"\nmax |score|: {abs_sorted[0][1]:+.2f}  at {abs_sorted[0][0]}  signal={abs_sorted[0][4]} conf={abs_sorted[0][5]}")

# distribution
import collections
buckets = collections.Counter()
for _, s, *_ in scores:
    if s <= -3: buckets["<=-3 (PUT)"] += 1
    elif s <= -1: buckets["-3<s<=-1"] += 1
    elif s < 1: buckets["-1<s<1"] += 1
    elif s < 3: buckets["1<=s<3"] += 1
    else: buckets[">=3 (CALL)"] += 1
print("\nscore distribution:")
for k in ["<=-3 (PUT)", "-3<s<=-1", "-1<s<1", "1<=s<3", ">=3 (CALL)"]:
    print(f"  {k:12s}: {buckets[k]}")
