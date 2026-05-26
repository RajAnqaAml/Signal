"""Verify the SHIPPED V3 code (app.py:_classify_v3_tier) classifies the
5-day live production data correctly.

This is the validation test the user asked for: does the actual code that
will run on Monday produce the expected results against the data we have?
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime
from zoneinfo import ZoneInfo
import db
from app import _classify_v3_tier, IST

DATES = ["2026-05-20", "2026-05-21", "2026-05-22", "2026-05-25", "2026-05-26"]
SYMBOLS = ["NIFTY", "BANKNIFTY"]
PT_TO_INR = {"NIFTY": 0.5 * 75, "BANKNIFTY": 0.5 * 15}


def fmt(ts_iso):
    return datetime.fromisoformat(ts_iso).astimezone(IST)


def classify_snap(snap, symbol):
    """Re-run the shipped V3 classifier against a stored snapshot."""
    sig = snap.get("signal") or "NEUTRAL"
    score = snap.get("score") or 0
    conf = snap.get("confidence") or 0
    oi_score = snap.get("oi_score") or 0
    trend_score = snap.get("trend_score") or 0
    reasons = snap.get("reasons") or []
    # contrarian_penalty: infer from reasons (the engine adds "Contrarian" to reasons)
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


def simulate_pnl(snap, future_snaps, symbol):
    """Simulate trade outcome: walk forward, find target/SL hit, return P&L."""
    direction = snap["signal"]
    if direction == "NEUTRAL":
        return 0.0, "NEUTRAL"
    entry_spot = snap["spot_price"]
    sign = 1 if direction == "CALL" else -1
    target_pts = 75 if symbol == "NIFTY" else 150
    sl_pts = 60 if symbol == "NIFTY" else 120
    last = None
    for r in future_snaps:
        delta = (r["spot_price"] - entry_spot) * sign
        if delta >= target_pts:
            return target_pts * PT_TO_INR[symbol], "TARGET"
        if delta <= -sl_pts:
            return -sl_pts * PT_TO_INR[symbol], "STOP"
        last = r
    if last is None:
        return 0.0, "NO-DATA"
    delta = (last["spot_price"] - entry_spot) * sign
    return delta * PT_TO_INR[symbol], "EOD"


print("=" * 90)
print("VERIFYING SHIPPED V3 CODE AGAINST 5-DAY LIVE PRODUCTION DATA")
print("=" * 90)
print()

grand_t1_pnl = 0
grand_t2_pnl = 0
grand_t3_avoided = 0
grand_current_pnl = 0
grand_t1_count = 0
grand_current_count = 0

for symbol in SYMBOLS:
    print(f"\n>>> {symbol} <<<")
    sym_t1 = []
    sym_t2 = []
    sym_t3 = []

    for d in DATES:
        rows = db.get_snapshots(d, symbol)
        if not rows:
            continue
        prev_sig = ""
        for i, snap in enumerate(rows):
            sig = snap.get("signal") or "NEUTRAL"
            is_transition = (sig in ("CALL", "PUT") and prev_sig != sig)
            if is_transition:
                push_tier, blocks = classify_snap(snap, symbol)
                pnl, outcome = simulate_pnl(snap, rows[i+1:], symbol)
                record = {
                    "date": d,
                    "ts": fmt(snap["ts"]),
                    "direction": sig,
                    "spot": snap["spot_price"],
                    "score": snap.get("score") or 0,
                    "conf": snap.get("confidence") or 0,
                    "tier": push_tier,
                    "blocks": blocks,
                    "pnl": pnl,
                    "outcome": outcome,
                }
                if push_tier == "TIER_1":
                    sym_t1.append(record)
                elif push_tier == "TIER_2":
                    sym_t2.append(record)
                else:
                    sym_t3.append(record)
            prev_sig = sig

    cur_count = len(sym_t1) + len(sym_t2) + len(sym_t3)
    cur_pnl = sum(r["pnl"] for r in sym_t1 + sym_t2 + sym_t3)
    t1_pnl = sum(r["pnl"] for r in sym_t1)
    t2_pnl = sum(r["pnl"] for r in sym_t2)
    t3_pnl_avoided = sum(r["pnl"] for r in sym_t3)

    print(f"\nCurrent engine total: {cur_count} fires, Net Rs {cur_pnl:+,.0f}")
    print(f"V3 TIER 1 (auto-push): {len(sym_t1)} fires, Net Rs {t1_pnl:+,.0f}")
    if sym_t1:
        print(f"  Win rate: {sum(1 for r in sym_t1 if r['pnl']>0)}/{len(sym_t1)} = "
              f"{sum(1 for r in sym_t1 if r['pnl']>0)/len(sym_t1)*100:.0f}%")
        for r in sym_t1:
            print(f"  {r['date']} {r['ts'].strftime('%H:%M')}  {r['direction']:>4}  "
                  f"spot={r['spot']:.2f} score={r['score']:+.1f} -> "
                  f"{r['outcome']:>7} Rs {r['pnl']:+,.0f}")
    print(f"\nV3 TIER 2 (dashboard watch, no push): {len(sym_t2)} fires, Net Rs {t2_pnl:+,.0f}")
    if sym_t2:
        for r in sym_t2:
            print(f"  {r['date']} {r['ts'].strftime('%H:%M')}  {r['direction']:>4}  "
                  f"score={r['score']:+.1f} -> Rs {r['pnl']:+,.0f}  | blocks: {'; '.join(r['blocks'])}")
    print(f"V3 TIER 3 (refused entirely): {len(sym_t3)} fires")
    profitable_refusals = sorted(sym_t3, key=lambda r: -r["pnl"])[:5]
    if profitable_refusals:
        print(f"  Top 5 profitable refusals:")
        for r in profitable_refusals:
            print(f"    {r['date']} {r['ts'].strftime('%H:%M')} {r['direction']:>4} score={r['score']:+.1f} -> Rs {r['pnl']:+,.0f} | blocks: {'; '.join(r['blocks'])}")
    print(f"  Would-have-lost-if-traded: Rs {t3_pnl_avoided:+,.0f}")
    print(f"  Wins/Losses if traded: "
          f"{sum(1 for r in sym_t3 if r['pnl']>0)}/{sum(1 for r in sym_t3 if r['pnl']<0)}")

    grand_t1_pnl += t1_pnl
    grand_t2_pnl += t2_pnl
    grand_t3_avoided += t3_pnl_avoided
    grand_current_pnl += cur_pnl
    grand_t1_count += len(sym_t1)
    grand_current_count += cur_count

print("\n" + "=" * 90)
print("VERIFICATION SUMMARY")
print("=" * 90)
print(f"Current engine: {grand_current_count} fires, Rs {grand_current_pnl:+,.0f}")
print(f"V3 TIER 1 (auto-push): {grand_t1_count} fires, Rs {grand_t1_pnl:+,.0f}")
print(f"V3 TIER 2 (watch only): Rs {grand_t2_pnl:+,.0f}")
print(f"V3 TIER 3 refused (P&L avoided): Rs {grand_t3_avoided:+,.0f}")
print()
print(f"Tier 1 alone captures: {grand_t1_pnl/abs(grand_current_pnl)*100:.0f}% of current engine's P&L")
print(f"Trade count reduction: {(1 - grand_t1_count/grand_current_count)*100:.0f}%")
