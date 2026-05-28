"""P&L report for today's signals.

For each transition (would-have-been-push), compute:
  - Entry spot, current spot, unrealized P&L
  - Max favorable / adverse since entry, with timing
  - Time to first +30 / +60 / +75 / +100 thresholds
  - What several exit rules would have realized
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import db

IST = ZoneInfo("Asia/Kolkata")
today = datetime.now(tz=IST).date().strftime("%Y-%m-%d")

# Per-spot-pt rupee yields (ATM, delta 0.5)
PT_TO_INR = {"NIFTY": 0.5 * 75, "BANKNIFTY": 0.5 * 15}  # 37.5 / 7.5

def fmt(ts_iso):
    return datetime.fromisoformat(ts_iso).astimezone(IST)


def collect_transitions(symbol):
    rows = db.get_snapshots(today, symbol)
    transitions = []
    prev_sig = ""
    for r in rows:
        sig = r.get("signal") or "NEUTRAL"
        if sig in ("CALL", "PUT") and prev_sig != sig:
            transitions.append(r)
        prev_sig = sig
    return rows, transitions


def analyze(entry, rows, symbol):
    """Compute milestones for one entry: current P&L, max FAV/ADV, threshold times."""
    direction = entry["signal"]
    sign = 1 if direction == "CALL" else -1
    entry_ts = fmt(entry["ts"])
    entry_spot = entry["spot_price"]
    future = [r for r in rows if fmt(r["ts"]) > entry_ts]

    out = {
        "max_fav": (0.0, None),
        "max_adv": (0.0, None),
        "current_delta": 0.0,
        "current_ts": entry_ts,
    }
    for thr in (30, 60, 75, 100, 150):
        out[f"fav_{thr}_at"] = None

    for r in future:
        delta = (r["spot_price"] - entry_spot) * sign
        ts = fmt(r["ts"])
        if delta > out["max_fav"][0]:
            out["max_fav"] = (delta, ts)
        if delta < out["max_adv"][0]:
            out["max_adv"] = (delta, ts)
        for thr in (30, 60, 75, 100, 150):
            if out[f"fav_{thr}_at"] is None and delta >= thr:
                out[f"fav_{thr}_at"] = (ts, r["spot_price"])
        out["current_delta"] = delta
        out["current_ts"] = ts
        out["current_spot"] = r["spot_price"]
    return out


print(f"=== P&L REPORT for {today} ===")
print(f"Time of report: {datetime.now(tz=IST).strftime('%H:%M:%S IST')}")
print()

for symbol in ("NIFTY", "BANKNIFTY"):
    print(f"╔══════════════════════════════════════════════════════════════════╗")
    print(f"║  {symbol}  (1 spot pt = Rs {PT_TO_INR[symbol]:.1f} per lot)                          ║")
    print(f"╚══════════════════════════════════════════════════════════════════╝")
    rows, txns = collect_transitions(symbol)
    if not txns:
        print(f"  No transitions / engine fires today.\n")
        continue

    total_unrealized = 0.0
    for i, t in enumerate(txns, 1):
        m = analyze(t, rows, symbol)
        entry_ts = fmt(t["ts"])
        entry_spot = t["spot_price"]
        direction = t["signal"]
        sign = 1 if direction == "CALL" else -1
        current_inr = m["current_delta"] * PT_TO_INR[symbol]
        max_fav_inr = m["max_fav"][0] * PT_TO_INR[symbol]
        max_adv_inr = m["max_adv"][0] * PT_TO_INR[symbol]
        held_min = (m["current_ts"] - entry_ts).total_seconds() / 60
        total_unrealized += current_inr

        print(f"\n  TRADE #{i}: {direction} @ {entry_ts.strftime('%H:%M:%S')}  Spot {entry_spot:.2f}")
        print(f"    Held: {held_min:.0f} min   "
              f"Latest spot: {m.get('current_spot', entry_spot):.2f}")
        print(f"    Current unrealized: {m['current_delta']:+.1f} pts  =  Rs {current_inr:+,.0f} per lot")
        if m['max_fav'][1]:
            print(f"    Peak favorable:    {m['max_fav'][0]:+.1f} pts @ {m['max_fav'][1].strftime('%H:%M')}  =  Rs {max_fav_inr:+,.0f}")
        if m['max_adv'][1]:
            print(f"    Peak adverse:      {m['max_adv'][0]:+.1f} pts @ {m['max_adv'][1].strftime('%H:%M')}  =  Rs {max_adv_inr:+,.0f}")
        print(f"    Threshold hit times (favorable side):")
        for thr in (30, 60, 75, 100, 150):
            info = m[f"fav_{thr}_at"]
            if info:
                ts, sp = info
                lag = (ts - entry_ts).total_seconds() / 60
                print(f"      +{thr:>3} pts at {ts.strftime('%H:%M')}  (+{lag:.0f} min)  Rs {thr * PT_TO_INR[symbol]:+,.0f}")
            else:
                print(f"      +{thr:>3} pts: not yet reached")

    print(f"\n  ─────────────────────────────────────────────────")
    print(f"  TOTAL UNREALIZED if you held ALL {len(txns)} trades to NOW: Rs {total_unrealized:+,.0f}")
    print(f"  (assumes 1 lot ATM option per signal, no exits taken)")
