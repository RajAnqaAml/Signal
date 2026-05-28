"""Compare snapshot timestamps + intervals between yesterday and today."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import db

IST = ZoneInfo("Asia/Kolkata")
today = datetime.now(tz=IST).date()
yesterday = today - timedelta(days=1)

for d in (yesterday, today):
    date_str = d.strftime("%Y-%m-%d")
    rows = db.get_snapshots(date_str, "NIFTY")
    print(f"\n=== {date_str} ({d.strftime('%A')}) — {len(rows)} NIFTY snaps ===")
    if not rows:
        continue
    prev_ts = None
    for i, r in enumerate(rows, 1):
        ts = datetime.fromisoformat(r["ts"]).astimezone(IST)
        gap = ""
        if prev_ts is not None:
            gap_min = (ts - prev_ts).total_seconds() / 60.0
            gap = f"  (+{gap_min:.1f} min)"
        # how far off from a 10-min mark? (i.e. minute mod 10 + seconds)
        slot_offset = (ts.minute % 10) + ts.second / 60.0
        # if closer to next slot, use negative
        if slot_offset > 5:
            slot_offset -= 10
        off_mark = f"  off-10min-mark: {slot_offset:+.1f}"
        print(f"  {i:2d}  {ts.strftime('%H:%M:%S')}{gap}{off_mark}")
        prev_ts = ts
