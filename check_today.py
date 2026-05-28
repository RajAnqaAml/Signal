"""Quick status check for today's run.
Shows snapshot count, timing pattern (did we capture 09:15 IST opening?),
engine fires, and notifications for both NIFTY and BANKNIFTY.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import Counter

import db

IST = ZoneInfo("Asia/Kolkata")
today = datetime.now(tz=IST).date().strftime("%Y-%m-%d")
now_str = datetime.now(tz=IST).strftime("%H:%M:%S")

print(f"=== STATUS for {today} (now {now_str} IST) ===\n")

for sym in ("NIFTY", "BANKNIFTY"):
    rows = db.get_snapshots(today, sym)
    print(f"### {sym} ###")
    if not rows:
        print(f"  No snapshots yet.\n")
        continue
    first = datetime.fromisoformat(rows[0]["ts"]).astimezone(IST)
    last = datetime.fromisoformat(rows[-1]["ts"]).astimezone(IST)
    print(f"  Snaps: {len(rows)}   First: {first.strftime('%H:%M:%S')}   Last: {last.strftime('%H:%M:%S')}")

    # How far off was first snap from 09:15 IST?
    market_open = first.replace(hour=9, minute=15, second=0, microsecond=0)
    delay_min = (first - market_open).total_seconds() / 60
    if delay_min < 0:
        print(f"  First snap LANDED BEFORE OPEN by {-delay_min:.1f} min (was skipped by is_market_open)")
    elif delay_min <= 2:
        print(f"  First snap captured the open (+{delay_min:.1f} min after 09:15) ✓")
    else:
        print(f"  First snap LATE by {delay_min:.1f} min after 09:15 IST")

    # Gap analysis
    gaps = []
    for a, b in zip(rows, rows[1:]):
        ga = datetime.fromisoformat(a["ts"])
        gb = datetime.fromisoformat(b["ts"])
        gaps.append((gb - ga).total_seconds() / 60)
    if gaps:
        gaps_sorted = sorted(gaps)
        median = gaps_sorted[len(gaps)//2]
        print(f"  Cadence: median {median:.1f} min, range [{min(gaps):.1f} - {max(gaps):.1f}]")

    # Signal verdict counts
    verdicts = Counter(r.get("signal") for r in rows)
    print(f"  Verdicts: {dict(verdicts)}")

    # Engine fires (non-NEUTRAL) and transitions (=notifications)
    triggered_count = 0
    notify_count = 0
    notifications = []
    prev_sig = ""
    for r in rows:
        sig = r.get("signal") or "NEUTRAL"
        if sig in ("CALL", "PUT"):
            triggered_count += 1
            if prev_sig != sig:
                notify_count += 1
                ts = datetime.fromisoformat(r["ts"]).astimezone(IST)
                notifications.append((ts, sig, r.get("score"), r.get("confidence"), r["spot_price"]))
        prev_sig = sig
    print(f"  Triggered (CALL/PUT): {triggered_count}   |   Transitions (notifications): {notify_count}")

    if notifications:
        print(f"  Notification timeline:")
        for ts, sig, score, conf, spot in notifications:
            print(f"    {ts.strftime('%H:%M:%S')}  {sig:>4}  score={score:+.2f}  conf={conf:.0f}%  spot={spot:.2f}")

    # Today's score range
    scores = [r.get("score") or 0 for r in rows]
    if scores:
        print(f"  Score range: min={min(scores):+.2f}  max={max(scores):+.2f}  median={sorted(scores)[len(scores)//2]:+.2f}")

    # Last spot
    last_spot = rows[-1]["spot_price"]
    print(f"  Latest spot: {last_spot:.2f} (at {last.strftime('%H:%M:%S')})")
    print()
