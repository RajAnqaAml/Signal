"""Deep analysis of EVERY snapshot today (not just transitions).
Looking for patterns, anomalies, missed signals, what-ifs.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict
import db
from app import _classify_v3_tier

IST = ZoneInfo("Asia/Kolkata")
TODAY = datetime.now(tz=IST).date().strftime("%Y-%m-%d")


def fmt(ts_iso):
    return datetime.fromisoformat(ts_iso).astimezone(IST)


def classify_snap(snap, symbol):
    sig = snap.get("signal") or "NEUTRAL"
    score = snap.get("score") or 0
    conf = snap.get("confidence") or 0
    oi_score = snap.get("oi_score") or 0
    trend_score = snap.get("trend_score") or 0
    reasons = snap.get("reasons") or []
    has_contrarian = any(("Contrarian" in r) or ("Sharp" in r) for r in reasons)
    contrarian_penalty = 0.5 if has_contrarian else 1.0
    spot = snap["spot_price"]
    spot_data = {
        "open": snap.get("spot_open") or spot,
        "high": snap.get("spot_high") or spot,
        "low": snap.get("spot_low") or spot,
    }
    ts = fmt(snap["ts"])
    return _classify_v3_tier(
        sig, score, conf, oi_score, trend_score, contrarian_penalty,
        spot, spot_data, symbol, ts, reasons,
    )


print(f"DEEP ANALYSIS — every snap today ({TODAY})")
print(f"Generated: {datetime.now(tz=IST).strftime('%H:%M:%S IST')}")
print("=" * 100)

for symbol in ("NIFTY", "BANKNIFTY"):
    rows = db.get_snapshots(TODAY, symbol)
    if not rows:
        continue
    # Filter to only market-hours snaps (drop after-hours test fires)
    rows = [r for r in rows if 9 <= fmt(r["ts"]).hour <= 15]
    print(f"\n>>> {symbol} <<<  {len(rows)} market-hours snapshots\n")

    # === Section 1: Score distribution ===
    print(f"  [1] SCORE DISTRIBUTION across the day")
    score_buckets = Counter()
    for r in rows:
        s = round(r.get("score") or 0)
        score_buckets[s] += 1
    for s in sorted(score_buckets.keys()):
        bar = "█" * score_buckets[s]
        marker = "  <-- FIRE THRESHOLD" if abs(s) == 3 else ("  <-- EXPIRY-DAY THRESHOLD" if abs(s) == 5 else "")
        print(f"    score={s:+d}: {score_buckets[s]:>3d}  {bar}{marker}")

    # === Section 2: Time when score peaked ===
    print(f"\n  [2] SCORE EXTREMES of the day")
    max_snap = max(rows, key=lambda r: r.get("score") or 0)
    min_snap = min(rows, key=lambda r: r.get("score") or 0)
    print(f"    MAX score: {max_snap.get('score'):+.1f} at {fmt(max_snap['ts']).strftime('%H:%M:%S')} "
          f"spot={max_snap['spot_price']:.2f}  sig={max_snap.get('signal')}")
    print(f"    MIN score: {min_snap.get('score'):+.1f} at {fmt(min_snap['ts']).strftime('%H:%M:%S')} "
          f"spot={min_snap['spot_price']:.2f}  sig={min_snap.get('signal')}")

    # === Section 3: Did score EVER reach +5 (expiry-day fire threshold)? ===
    print(f"\n  [3] EXPIRY-DAY FIREABILITY — did score ever reach |5|?")
    extreme_snaps = [r for r in rows if abs(r.get("score") or 0) >= 5]
    if extreme_snaps:
        print(f"    YES — {len(extreme_snaps)} snaps with |score|>=5")
        for s in extreme_snaps[:5]:
            t = fmt(s["ts"]).strftime("%H:%M:%S")
            print(f"      {t}  score={s.get('score'):+.1f}  spot={s['spot_price']:.2f}")
        if len(extreme_snaps) > 5:
            print(f"      ... and {len(extreme_snaps)-5} more")
    else:
        print(f"    NO — engine never reached |score|>=5 today.")
        print(f"      This confirms expiry-day Gate 8 was the correct call.")

    # === Section 4: OI Score evolution — when did it flip? ===
    print(f"\n  [4] OI SCORE FLIPS (the engine's most predictive factor)")
    prev_oi = None
    oi_flips = []
    for r in rows:
        oi = r.get("oi_score") or 0
        if prev_oi is not None and ((prev_oi > 0 and oi <= 0) or (prev_oi < 0 and oi >= 0)):
            oi_flips.append({
                "ts": fmt(r["ts"]),
                "spot": r["spot_price"],
                "prev_oi": prev_oi,
                "new_oi": oi,
                "score": r.get("score") or 0,
            })
        prev_oi = oi
    print(f"    Total OI flips: {len(oi_flips)}")
    for f in oi_flips[:8]:
        direction = "BULLISH" if f["new_oi"] > 0 else ("BEARISH" if f["new_oi"] < 0 else "NEUTRAL")
        print(f"      {f['ts'].strftime('%H:%M:%S')}  oi {f['prev_oi']:+.0f} -> {f['new_oi']:+.0f}  "
              f"({direction})  spot={f['spot']:.2f}  score={f['score']:+.1f}")

    # === Section 5: All transitions (would-be pushes) with V3 verdict ===
    print(f"\n  [5] ALL TRANSITIONS today with V3 verdict")
    prev_sig = ""
    transitions = []
    for i, r in enumerate(rows):
        sig = r.get("signal") or "NEUTRAL"
        if sig in ("CALL", "PUT") and prev_sig != sig:
            tier, blocks = classify_snap(r, symbol)
            transitions.append({
                "ts": fmt(r["ts"]),
                "direction": sig,
                "spot": r["spot_price"],
                "score": r.get("score") or 0,
                "tier": tier,
                "blocks": blocks,
            })
        prev_sig = sig
    for t in transitions:
        block_str = ("BLOCKED: " + "; ".join(t["blocks"][:2])) if t["blocks"] else "ALL GATES PASS"
        print(f"      {t['ts'].strftime('%H:%M:%S')}  {t['direction']:>4}  spot={t['spot']:.2f}  "
              f"score={t['score']:+.1f}  -> {t['tier']:<7}  {block_str}")

    # === Section 6: What-if analysis — would V3 fire on a non-expiry day? ===
    print(f"\n  [6] WHAT-IF — would V3 fire any of today's transitions on a non-expiry day?")
    print(f"      (Removing G8 from the analysis, keeping all other gates)")
    would_fire_count = 0
    for t in transitions:
        non_g8_blocks = [b for b in t["blocks"] if not b.startswith("G8")]
        if not non_g8_blocks:
            would_fire_count += 1
            print(f"      ✓ {t['ts'].strftime('%H:%M:%S')}  {t['direction']:>4}  spot={t['spot']:.2f}  "
                  f"score={t['score']:+.1f}  -- would fire if not expiry day")
    if would_fire_count == 0:
        print(f"      None — even without Gate 8, every transition failed other gates too")

    # === Section 7: Day's spot path with major moves ===
    print(f"\n  [7] SPOT TRAJECTORY — major moves of the day")
    day_open = rows[0]["spot_price"]
    day_high_snap = max(rows, key=lambda r: r["spot_price"])
    day_low_snap = min(rows, key=lambda r: r["spot_price"])
    day_close = rows[-1]["spot_price"]
    print(f"      Open:  {day_open:.2f}  ({fmt(rows[0]['ts']).strftime('%H:%M')})")
    print(f"      High:  {day_high_snap['spot_price']:.2f}  ({fmt(day_high_snap['ts']).strftime('%H:%M')})  "
          f"+{day_high_snap['spot_price'] - day_open:.0f} pts from open")
    print(f"      Low:   {day_low_snap['spot_price']:.2f}  ({fmt(day_low_snap['ts']).strftime('%H:%M')})  "
          f"{day_low_snap['spot_price'] - day_open:+.0f} pts from open")
    print(f"      Close: {day_close:.2f}  ({day_close - day_open:+.0f} pts from open)")
    intraday_range_pct = (day_high_snap['spot_price'] - day_low_snap['spot_price']) / day_open * 100
    print(f"      Intraday range: {intraday_range_pct:.2f}% (relative to open)")

    # === Section 8: Score velocity — did score build before transitions? ===
    print(f"\n  [8] SCORE VELOCITY before each transition (5-min before)")
    for t in transitions:
        # Find snap 1 step before transition
        ts_target = t["ts"]
        prior = None
        for r in rows:
            r_ts = fmt(r["ts"])
            if r_ts < ts_target and (prior is None or r_ts > fmt(prior["ts"])):
                prior = r
        if prior:
            prior_score = prior.get("score") or 0
            velocity = t["score"] - prior_score
            print(f"      {t['ts'].strftime('%H:%M:%S')}  {t['direction']}  "
                  f"prev_score={prior_score:+.1f}  now={t['score']:+.1f}  "
                  f"velocity={velocity:+.1f}")

print("\n" + "=" * 100)
print("KEY FINDINGS — patterns we should learn from")
print("=" * 100)

for symbol in ("NIFTY", "BANKNIFTY"):
    rows = db.get_snapshots(TODAY, symbol)
    if not rows:
        continue
    rows = [r for r in rows if 9 <= fmt(r["ts"]).hour <= 15]

    # Did NIFTY ever match the BN bearish move?
    intraday_pcts = [(fmt(r["ts"]), (r["spot_price"] - rows[0]["spot_price"]) / rows[0]["spot_price"] * 100) for r in rows]
    max_pct = max(intraday_pcts, key=lambda x: x[1])
    min_pct = min(intraday_pcts, key=lambda x: x[1])
    print(f"\n{symbol}: intraday range {min_pct[1]:+.2f}% (low @{min_pct[0].strftime('%H:%M')}) "
          f"to {max_pct[1]:+.2f}% (high @{max_pct[0].strftime('%H:%M')})")
