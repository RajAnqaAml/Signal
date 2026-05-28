"""Quick diagnostic: list snapshot timings + signal verdicts for the last 2 trading days."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import Counter

import db

IST = ZoneInfo("Asia/Kolkata")
today = datetime.now(tz=IST).date()
yesterday = today - timedelta(days=1)

for d in (yesterday, today):
    date_str = d.strftime("%Y-%m-%d")
    print(f"\n=== {date_str} ({d.strftime('%A')}) ===")
    for sym in ("NIFTY", "BANKNIFTY"):
        rows = db.get_snapshots(date_str, sym)
        print(f"\n  {sym}: {len(rows)} snapshots")
        if not rows:
            continue
        first_ts = rows[0]["ts"]
        last_ts = rows[-1]["ts"]
        verdicts = Counter(r.get("signal") for r in rows)
        print(f"    first: {first_ts}")
        print(f"    last:  {last_ts}")
        print(f"    verdicts: {dict(verdicts)}")
        # gaps between consecutive snapshots (in minutes)
        gaps = []
        for a, b in zip(rows, rows[1:]):
            ta = datetime.fromisoformat(a["ts"])
            tb = datetime.fromisoformat(b["ts"])
            gaps.append((tb - ta).total_seconds() / 60.0)
        if gaps:
            print(f"    gap min/median/max (min): {min(gaps):.1f} / {sorted(gaps)[len(gaps)//2]:.1f} / {max(gaps):.1f}")
        # show non-NEUTRAL signals if any
        non_neutral = [r for r in rows if r.get("signal") and r["signal"] != "NEUTRAL"]
        if non_neutral:
            print(f"    NON-NEUTRAL count: {len(non_neutral)}")
            for r in non_neutral[:5]:
                print(f"      {r['ts']} {r['signal']} conf={r.get('confidence')} score={r.get('score')}")
        else:
            print(f"    NON-NEUTRAL count: 0  (all NEUTRAL)")
        # show a sample row's score components
        sample = rows[len(rows)//2]
        print(f"    sample mid-day [{sample['ts']}]: signal={sample['signal']} score={sample.get('score')} "
              f"trend={sample.get('trend_score')} oi={sample.get('oi_score')} gap_w={sample.get('gap_weight')} "
              f"evidence={sample.get('evidence_quality')}")
