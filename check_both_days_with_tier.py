"""Both-day tables with Triggered + Notification columns.

Notification logic (recorder.py:67-83):
    push when ALL true:
       1. symbol == NIFTY (BANKNIFTY suppressed)
       2. current signal != NEUTRAL (engine fired)
       3. previous signal != current signal (transition only)
       4. ntfy is configured

Engine fires (Triggered=YES) when:
       |score| >= 3 AND not (sharp price/OI conflict)
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import db

IST = ZoneInfo("Asia/Kolkata")
# Hard-pin the two trading days we've been discussing:
thursday = datetime(2026, 5, 21, tzinfo=IST).date()
friday = datetime(2026, 5, 22, tzinfo=IST).date()

ENGINE_TGT = 75
ENGINE_SL = 60
PT_TO_INR = 0.50 * 75

def fmt_ist(ts_str):
    return datetime.fromisoformat(ts_str).astimezone(IST).strftime("%H:%M")

def tier_of(row):
    score = row.get("score") or 0
    conf = row.get("confidence") or 0
    oi = abs(row.get("oi_score") or 0)
    reasons = row.get("reasons") or []
    has_contrarian = any(("Contrarian" in r) or ("Sharp" in r) for r in reasons)
    if abs(score) >= 4 and conf >= 48 and oi >= 2 and not has_contrarian:
        return "GREEN"
    if abs(score) >= 3 and conf >= 30 and not has_contrarian:
        return "YELLOW"
    return "RED"

def walk(entry_spot, future_rows, direction):
    sign = 1 if direction == "CALL" else -1
    last = None
    for r in future_rows:
        d = (r["spot_price"] - entry_spot) * sign
        if d >= ENGINE_TGT and d <= -ENGINE_SL:
            return ("both?", r, 0.0)
        if d >= ENGINE_TGT:
            return ("target", r, ENGINE_TGT * PT_TO_INR)
        if d <= -ENGINE_SL:
            return ("stop", r, -ENGINE_SL * PT_TO_INR)
        last = r
    if last is None:
        return ("no-data", None, 0.0)
    eod = (last["spot_price"] - entry_spot) * sign
    return ("eod", last, eod * PT_TO_INR)

def report(date_obj):
    date_str = date_obj.strftime("%Y-%m-%d")
    rows = db.get_snapshots(date_str, "NIFTY")
    print(f"\n=== {date_str} ({date_obj.strftime('%A')}) - {len(rows)} NIFTY snaps ===\n")
    print(f"{'#':>2} {'time':<6} {'spot':>9} {'sc':>4} {'conf':>5} "
          f"{'sig':>7} {'trig':<4} {'notify':<6} {'tier':<7} {'dir':>4} "
          f"{'outcome':<7} {'P&L Rs':>9}")
    print("-" * 95)

    triggered_count = 0
    notify_count = 0
    notify_pl = 0.0
    prev_sig = ""  # tracks previous signal for transition detection

    for idx, r in enumerate(rows, start=1):
        score = r.get("score") or 0
        conf = r.get("confidence") or 0
        actual_sig = r.get("signal") or "NEUTRAL"
        spot = r["spot_price"]
        future = rows[idx:]
        tier = tier_of(r)

        triggered = actual_sig in ("CALL", "PUT")
        # Notification: triggered AND previous signal different from current
        notification = triggered and (prev_sig != actual_sig)

        if triggered: triggered_count += 1
        if notification: notify_count += 1

        # Hypothetical entry direction follows score sign (not the engine gate)
        if score == 0:
            print(f"{idx:>2} {fmt_ist(r['ts']):<6} {spot:>9.2f} {score:>+4.0f} "
                  f"{conf:>5.1f} {actual_sig:>7} "
                  f"{'NO':<4} {'-':<6} {tier:<7} {'-':>4} {'flat':<7} {0:>+9.0f}")
            prev_sig = actual_sig
            continue

        direction = "CALL" if score > 0 else "PUT"
        outcome, hit_row, pl = walk(spot, future, direction)
        if notification:
            notify_pl += pl

        trig_str = "YES" if triggered else "NO"
        notif_str = "YES" if notification else "-"

        print(f"{idx:>2} {fmt_ist(r['ts']):<6} {spot:>9.2f} {score:>+4.0f} "
              f"{conf:>5.1f} {actual_sig:>7} "
              f"{trig_str:<4} {notif_str:<6} {tier:<7} {direction:>4} "
              f"{outcome:<7} {pl:>+9.0f}")

        prev_sig = actual_sig

    print("-" * 95)
    print(f"Triggered (engine fired non-NEUTRAL): {triggered_count} / {len(rows)}")
    print(f"Notifications (transitions only)    : {notify_count}")
    print(f"P&L from acting only on notifications: Rs {notify_pl:+,.0f}")

report(thursday)
report(friday)
