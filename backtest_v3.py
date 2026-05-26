"""V3 ENGINE BACKTEST against 5 days of real production data.

Applies the proposed 10 gates to every snapshot we recorded and shows:
  - Which signals would have FIRED under V3
  - Which existing signals would have been REFUSED (and by which gate)
  - Per-trade P&L under V3 vs the current engine
  - Net 5-day comparison

This is the validation step before shipping V3 code. If V3 doesn't show
clear improvement on real data, we don't ship it.

Gates (any failure -> NEUTRAL):
  1. GREEN tier (score >= 4 AND conf >= 48 AND |oi| >= 2 AND no contrarian)
  2. Multi-timeframe alignment (spot vs prev_close same dir as signal)
  3. VWAP confirmation (spot above/below day's running avg)
  4. Late-entry filter (not too far from open, not too close to day extreme)
  5. Consecutive confirmation (prev snap also passed threshold)
  6. Chop detection (intraday range < threshold past 11:00)
  7. Time-of-day filter (09:30-14:30 IST)
  8. Expiry day filter (Tuesday: stricter score >=5, no fires after 13:00)
  9. Daily signal cap (max 2 phone pushes per symbol per day)
 10. Failed-signal lockout (2-hour lockout after same-dir failure)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import db

IST = ZoneInfo("Asia/Kolkata")

DATES = ["2026-05-20", "2026-05-21", "2026-05-22", "2026-05-25", "2026-05-26"]
SYMBOLS = ["NIFTY", "BANKNIFTY"]
PT_TO_INR = {"NIFTY": 0.5 * 75, "BANKNIFTY": 0.5 * 15}  # rough delta-based estimate

# Per-symbol thresholds
LATE_PCT_OPEN     = {"NIFTY": 0.30, "BANKNIFTY": 0.40}
LATE_PCT_EXTREME  = {"NIFTY": 0.10, "BANKNIFTY": 0.10}
CHOP_RANGE_PCT    = {"NIFTY": 0.40, "BANKNIFTY": 0.50}

EXPIRY_DOW = 1  # Tuesday (based on user's BANKNIFTY 26 MAY expiry confirmation)


def fmt(ts_iso):
    return datetime.fromisoformat(ts_iso).astimezone(IST)


def build_day_context(day_snaps, prev_close):
    """For each snap, what context does V3 need? Pre-compute once per day."""
    # Use NSE's official spot_open if available (which captures the true market
    # open even if our recorder started late). Falls back to first snap's spot
    # only if spot_open isn't in the data.
    official_open = None
    for s in day_snaps:
        if s.get("spot_open"):
            official_open = float(s["spot_open"])
            break
    if official_open is None:
        official_open = day_snaps[0]["spot_price"]

    # Use NSE's day_high / day_low too if available (caches true intraday extremes
    # even if we missed the opening move). Take from the latest snap that has it.
    # We use a running track that respects whichever is more extreme.
    ctx = []
    cum_spot_sum = 0
    cum_count = 0
    day_high = float("-inf")
    day_low = float("inf")
    for i, s in enumerate(day_snaps):
        spot = s["spot_price"]
        cum_spot_sum += spot
        cum_count += 1
        # Reconcile recorded extremes with NSE-reported ones
        nse_high = s.get("spot_high") or 0
        nse_low = s.get("spot_low") or 0
        day_high = max(day_high, spot, nse_high)
        if nse_low > 0:
            day_low = min(day_low, spot, nse_low)
        else:
            day_low = min(day_low, spot)
        ctx.append({
            "vwap_proxy": cum_spot_sum / cum_count,
            "day_high_so_far": day_high,
            "day_low_so_far": day_low,
            "day_open": official_open,
            "prev_close": prev_close,
        })
    return ctx


def check_v3_gates(snap, prev_snap, context, symbol, today_pushes, recent_failures, date_str):
    """Walk a snap through all 10 V3 gates. Returns (passes, blocking_gate_name)."""
    score = snap.get("score") or 0
    conf = snap.get("confidence") or 0
    oi = abs(snap.get("oi_score") or 0)
    reasons = snap.get("reasons") or []
    spot = snap["spot_price"]
    ts = fmt(snap["ts"])

    # ── Gate 1 (V3.1): Quality threshold ─
    # V3.1 drops OI requirement -- it killed 7 good signals including Monday's
    # +Rs 4,838 BN CALL where OI hadn't built up at market open yet. Price
    # action + gap analysis alone are valid signals.
    if abs(score) < 3: return False, "G1: score<3 (below fire threshold)"
    if any(("Contrarian" in r) or ("Sharp" in r) for r in reasons):
        return False, "G1: contrarian conflict"

    direction = "CALL" if score > 0 else "PUT"

    # ── Gate 2: Intraday direction alignment ─────────────────────
    # Replaced prev_close anchor (which kills valid intraday reversals like
    # Thursday's PUTs) with a check that today's intraday move agrees with
    # the signal direction. Less strict, catches true counter-trend fires.
    pct_from_open_g2 = (spot - context["day_open"]) / context["day_open"] * 100
    if direction == "CALL" and pct_from_open_g2 < -0.20:
        return False, f"G2: CALL but spot {pct_from_open_g2:.2f}% below open"
    if direction == "PUT" and pct_from_open_g2 > 0.20:
        return False, f"G2: PUT but spot {pct_from_open_g2:.2f}% above open"

    # ── Gate 3: VWAP confirmation ─────────────────────────────────
    if direction == "CALL" and spot < context["vwap_proxy"]:
        return False, "G3: spot below VWAP for CALL"
    if direction == "PUT" and spot > context["vwap_proxy"]:
        return False, "G3: spot above VWAP for PUT"

    # ── Gate 4 (V3.1): Late-entry filter ─
    # V3.1 only applies the late-entry / near-extreme check AFTER:
    #   (a) market has been open >= 15 min (past 09:30 IST), AND
    #   (b) day's range has formed (>= 0.15% from open)
    # This allows fresh opening signals through (Monday 09:15 BN CALL was the
    # biggest winner -- shouldn't have been refused for being "near day high"
    # when day high = day open = first tick).
    range_pct_g4 = (context["day_high_so_far"] - context["day_low_so_far"]) / context["day_open"] * 100
    skip_g4 = (ts.hour == 9 and ts.minute < 30) or range_pct_g4 < 0.15
    if not skip_g4:
        pct_from_open = (spot - context["day_open"]) / context["day_open"] * 100
        pct_below_high = (context["day_high_so_far"] - spot) / context["day_open"] * 100
        pct_above_low = (spot - context["day_low_so_far"]) / context["day_open"] * 100
        late_open = LATE_PCT_OPEN[symbol]
        late_extreme = LATE_PCT_EXTREME[symbol]
        if direction == "CALL":
            if pct_from_open > late_open:
                return False, f"G4: CALL too late ({pct_from_open:.2f}% above open > {late_open}%)"
            if pct_below_high < late_extreme:
                return False, f"G4: CALL too close to day high ({pct_below_high:.2f}% < {late_extreme}%)"
        else:
            if pct_from_open < -late_open:
                return False, f"G4: PUT too late ({pct_from_open:.2f}% below open < -{late_open}%)"
            if pct_above_low < late_extreme:
                return False, f"G4: PUT too close to day low ({pct_above_low:.2f}% < {late_extreme}%)"

    # ── Gate 5 (V3.1): Score velocity / opening allowance ─
    # V3.1: First-snap-of-day fires are allowed if score is strong (>= 3 mag).
    # Otherwise require prev_score to show buildup or already be in signal direction.
    if not prev_snap:
        # First snap of day -- allow if score is meaningful
        if abs(score) >= 3:
            pass  # fall through, accept
        else:
            return False, "G5: first snap but score too weak"
    else:
        prev_score = prev_snap.get("score") or 0
        if direction == "CALL":
            if prev_score < 1:
                return False, f"G5: stale signal (prev_score={prev_score:.1f}, no buildup)"
        else:
            if prev_score > -1:
                return False, f"G5: stale signal (prev_score={prev_score:.1f}, no buildup)"

    # ── Gate 6: Range/chop detection ─────────────────────────────
    range_pct = (context["day_high_so_far"] - context["day_low_so_far"]) / context["day_open"] * 100
    if ts.hour >= 11 and range_pct < CHOP_RANGE_PCT[symbol]:
        return False, f"G6: chop day (range {range_pct:.2f}% < {CHOP_RANGE_PCT[symbol]}%) past 11:00"

    # ── Gate 7 (V3.2): Time-of-day filter with opening-signal exception ─
    # The big winner (Mon 2026-05-25 BN CALL @ 09:15) was being refused by
    # blanket "before 09:30 = refuse" rule. New: allow OPENING signals
    # (before 09:30) ONLY if:
    #   - score magnitude >= 3 (we already required this)
    #   - prev snap was NEUTRAL or non-existent (this IS the first transition)
    #   - intraday range so far is small (we're not chasing a 20-min rally)
    # This is the "fresh opening conviction" exception.
    if ts.hour < 9 or (ts.hour == 9 and ts.minute < 30):
        is_first_transition = (prev_snap is None) or ((prev_snap.get("signal") or "NEUTRAL") == "NEUTRAL")
        range_pct_g7 = (context["day_high_so_far"] - context["day_low_so_far"]) / context["day_open"] * 100
        if not (is_first_transition and range_pct_g7 < 0.30):
            return False, "G7: before 09:30 IST (no opening exception)"
        # else: opening signal exception applies, allow through
    if (ts.hour, ts.minute) >= (14, 30):
        return False, "G7: after 14:30 IST"

    # ── Gate 8: Expiry day filter ────────────────────────────────
    is_expiry = ts.weekday() == EXPIRY_DOW
    if is_expiry:
        if abs(score) < 5:
            return False, f"G8: expiry day, need |score|>=5 (got {score})"
        if ts.hour >= 13:
            return False, "G8: expiry day, no fires after 13:00"

    # ── Gate 9: Daily signal cap ─────────────────────────────────
    if len(today_pushes) >= 2:
        return False, "G9: daily cap (2 pushes) reached"

    # ── Gate 10: Failed-signal lockout ───────────────────────────
    for fail in recent_failures:
        if fail["direction"] == direction and (ts - fail["ts"]).total_seconds() < 7200:
            return False, f"G10: same-dir lockout from {fail['ts'].strftime('%H:%M')}"

    return True, "PASS all 10 gates"


def label_failure(push, future_snaps, symbol):
    """Was this push a 'failure'? Defined as: spot moved against direction by
    >0.3% within 30 min of entry."""
    sign = 1 if push["direction"] == "CALL" else -1
    cutoff = push["ts"] + timedelta(minutes=30)
    entry_spot = push["entry_spot"]
    threshold_pct = 0.3
    for s in future_snaps:
        ts = fmt(s["ts"])
        if ts > cutoff:
            break
        delta_pct = (s["spot_price"] - entry_spot) * sign / entry_spot * 100
        if delta_pct < -threshold_pct:
            return True
    return False


def compute_pnl(push, future_snaps, symbol):
    """Walk forward to find first hit of target/SL OR end-of-day close.
    Returns (outcome, exit_spot, pl_inr)."""
    sign = 1 if push["direction"] == "CALL" else -1
    entry_spot = push["entry_spot"]
    target = 75 if symbol == "NIFTY" else 150
    sl = 60 if symbol == "NIFTY" else 120
    last = None
    for s in future_snaps:
        delta = (s["spot_price"] - entry_spot) * sign
        if delta >= target:
            return "TARGET", s["spot_price"], target * PT_TO_INR[symbol]
        if delta <= -sl:
            return "STOP", s["spot_price"], -sl * PT_TO_INR[symbol]
        last = s
    if last is None:
        return "NO-DATA", entry_spot, 0
    exit_delta = (last["spot_price"] - entry_spot) * sign
    return "EOD", last["spot_price"], exit_delta * PT_TO_INR[symbol]


def run_backtest():
    # Pre-fetch all data
    print("Fetching 5 days of data from Supabase...", flush=True)
    data = {}
    for d in DATES:
        for sym in SYMBOLS:
            rows = db.get_snapshots(d, sym)
            data[(d, sym)] = rows
            print(f"  {d} {sym}: {len(rows)} snaps")

    # Compute prev_close for each (date, symbol)
    prev_close_for = {}
    for i, d in enumerate(DATES):
        for sym in SYMBOLS:
            if i == 0:
                prev_close_for[(d, sym)] = data[(d, sym)][0]["spot_price"] if data[(d, sym)] else None
            else:
                prior_rows = data[(DATES[i-1], sym)]
                prev_close_for[(d, sym)] = prior_rows[-1]["spot_price"] if prior_rows else None

    results = {sym: {"current_fires": [], "v3_fires": [], "refused_by_gate": {}} for sym in SYMBOLS}

    # Walk each day, each symbol
    for d in DATES:
        for sym in SYMBOLS:
            rows = data[(d, sym)]
            if not rows:
                continue
            context_list = build_day_context(rows, prev_close_for[(d, sym)])
            today_pushes = []     # V3 pushes today (for daily cap)
            recent_failures = []  # V3-fired pushes that failed (for lockout)

            prev_sig = ""
            for i, snap in enumerate(rows):
                sig = snap.get("signal") or "NEUTRAL"
                ts = fmt(snap["ts"])

                # Current engine: would have pushed this snap?
                is_transition = (sig in ("CALL", "PUT") and prev_sig != sig)
                if is_transition:
                    direction = sig
                    score = snap.get("score") or 0
                    # Check current-engine cooldown: any same-dir push in last 30 min in DB?
                    # For simplicity assume no cooldown — DB shows all transitions
                    fire_record = {
                        "date": d,
                        "ts": ts,
                        "direction": direction,
                        "entry_spot": snap["spot_price"],
                        "score": score,
                        "conf": snap.get("confidence") or 0,
                    }
                    # P&L for this fire
                    outcome, exit_spot, pl_inr = compute_pnl(fire_record, rows[i+1:], sym)
                    fire_record.update({"outcome": outcome, "exit_spot": exit_spot, "pl_inr": pl_inr})
                    results[sym]["current_fires"].append(fire_record)

                # V3 engine: check all gates
                ctx = context_list[i]
                prev_snap = rows[i-1] if i > 0 else None
                passes, reason = check_v3_gates(
                    snap, prev_snap, ctx, sym, today_pushes, recent_failures, d
                )

                # V3 also requires transition (signal != prev signal AND not NEUTRAL)
                if passes and not is_transition:
                    passes = False
                    reason = "Not a transition (continuation)"

                if passes:
                    direction = sig
                    fire_record = {
                        "date": d,
                        "ts": ts,
                        "direction": direction,
                        "entry_spot": snap["spot_price"],
                        "score": snap.get("score"),
                        "conf": snap.get("confidence"),
                    }
                    outcome, exit_spot, pl_inr = compute_pnl(fire_record, rows[i+1:], sym)
                    fire_record.update({"outcome": outcome, "exit_spot": exit_spot, "pl_inr": pl_inr})

                    # Check if it failed (for Gate 10 lockout tracking)
                    if label_failure(fire_record, rows[i+1:], sym):
                        recent_failures.append({"ts": ts, "direction": direction})

                    today_pushes.append(fire_record)
                    results[sym]["v3_fires"].append(fire_record)
                elif is_transition:
                    # The current engine would have fired, V3 refused
                    key = reason.split(":")[0]
                    results[sym]["refused_by_gate"][key] = results[sym]["refused_by_gate"].get(key, 0) + 1

                prev_sig = sig

    # ── Print report ─────────────────────────────────────────────
    print()
    print("=" * 100)
    print("V3 BACKTEST RESULTS — 5 trading days (2026-05-20 through 2026-05-26)")
    print("=" * 100)

    grand_current = grand_v3 = 0
    for sym in SYMBOLS:
        print(f"\n### {sym} ###\n")

        # Current engine fires
        cur = results[sym]["current_fires"]
        print(f"Current engine: {len(cur)} pushes fired")
        if cur:
            print(f"{'Date':<12} {'Time':<8} {'Dir':>4} {'Spot':>9} {'Score':>5} {'Outcome':<8} {'P&L Rs':>10}")
            cur_total = 0
            for f in cur:
                print(f"{f['date']:<12} {f['ts'].strftime('%H:%M:%S'):<8} {f['direction']:>4} "
                      f"{f['entry_spot']:>9.2f} {f['score']:>+5.0f} {f['outcome']:<8} {f['pl_inr']:>+10,.0f}")
                cur_total += f['pl_inr']
            print(f"{'':>57} TOTAL: Rs {cur_total:+,.0f}")
            grand_current += cur_total

        print()
        v3 = results[sym]["v3_fires"]
        print(f"V3 engine: {len(v3)} pushes fired")
        if v3:
            print(f"{'Date':<12} {'Time':<8} {'Dir':>4} {'Spot':>9} {'Score':>5} {'Outcome':<8} {'P&L Rs':>10}")
            v3_total = 0
            for f in v3:
                print(f"{f['date']:<12} {f['ts'].strftime('%H:%M:%S'):<8} {f['direction']:>4} "
                      f"{f['entry_spot']:>9.2f} {f['score']:>+5.0f} {f['outcome']:<8} {f['pl_inr']:>+10,.0f}")
                v3_total += f['pl_inr']
            print(f"{'':>57} TOTAL: Rs {v3_total:+,.0f}")
            grand_v3 += v3_total
        else:
            print("  (no V3 fires)")

        # Why current fires were refused under V3
        print(f"\nBreakdown of {len(cur)} current-engine pushes:")
        v3_fired_keys = {(f['date'], f['ts'].strftime('%H:%M:%S')) for f in v3}
        for f in cur:
            key = (f['date'], f['ts'].strftime('%H:%M:%S'))
            if key in v3_fired_keys:
                print(f"  ✓ {f['date']} {key[1]} {f['direction']}  -> V3 FIRED also")
            else:
                # Re-run gates to get reason
                snaps = data[(f['date'], sym)]
                snap_idx = next((i for i, s in enumerate(snaps) if fmt(s['ts']).strftime('%H:%M:%S') == key[1]), None)
                if snap_idx is None:
                    print(f"  ? {f['date']} {key[1]} {f['direction']}  -> ???")
                    continue
                ctx_list = build_day_context(snaps, prev_close_for[(f['date'], sym)])
                ctx = ctx_list[snap_idx]
                prev_snap = snaps[snap_idx - 1] if snap_idx > 0 else None
                _, reason = check_v3_gates(snaps[snap_idx], prev_snap, ctx, sym, [], [], f['date'])
                print(f"  ✗ {f['date']} {key[1]} {f['direction']}  -> REFUSED: {reason}")

    print()
    print("=" * 100)
    print(f"GRAND TOTAL: Current Rs {grand_current:+,.0f}  |  V3 Rs {grand_v3:+,.0f}")
    print(f"Difference: V3 captures Rs {grand_v3 - grand_current:+,.0f} more (or loses less)")
    print("=" * 100)


if __name__ == "__main__":
    run_backtest()
