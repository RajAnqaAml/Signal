"""Backtest CLI. Reads snapshots from Supabase (preferred) or local JSONL fallback.

Examples:
    # Single trade: was the 10:00 NIFTY signal correct by 10:30?
    python backtest.py --date 2026-05-20 --entry 10:00 --check 10:30 --symbol NIFTY

    # Single trade with default 30-min horizon
    python backtest.py --date 2026-05-20 --entry 10:00

    # Full-day summary: how did every non-neutral NIFTY signal perform over 30 minutes?
    python backtest.py --date 2026-05-20 --symbol NIFTY

    # 15-min horizon, both symbols
    python backtest.py --date 2026-05-20 --horizon 15 --symbol BANKNIFTY

Verdict logic:
    For CALL: T2 hit > T1 hit > price up = win-direction; SL hit = loss; price down = drawdown.
    For PUT: T2 hit > T1 hit > price down = win-direction; SL hit = loss; price up = drawdown.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import db as _db
except ImportError:
    _db = None

IST = ZoneInfo("Asia/Kolkata")
SNAPSHOT_DIR = "snapshots"


def _row_to_legacy_snap(row, symbol):
    """Convert a Supabase snapshots row -> legacy JSONL snap shape so the rest
    of this CLI stays unchanged."""
    ts_iso = row["ts"]
    if ts_iso.endswith("Z"):
        ts_iso = ts_iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts_iso).astimezone(IST)
    return {
        "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S IST"),
        "vix": {"value": row.get("vix"), "change": row.get("vix_change")},
        "data": {symbol: row.get("raw_payload") or {}},
    }


def load_snapshots(date_str, symbol):
    # Preferred path: Supabase
    if _db is not None and _db.is_configured():
        rows = _db.get_snapshots(date_str, symbol)
        if rows:
            print(f"(source: Supabase, {len(rows)} rows for {symbol})")
            return [_row_to_legacy_snap(r, symbol) for r in rows]
        # fall through to JSONL if DB returned nothing (e.g., backfill not yet done)

    # Fallback: local JSONL
    path = os.path.join(SNAPSHOT_DIR, f"{date_str}.jsonl")
    if not os.path.exists(path):
        sys.exit(f"No data found for {date_str}. "
                f"Tried Supabase (empty/unconfigured) and {path} (missing).")
    snaps = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            snaps.append(json.loads(line))
    if not snaps:
        sys.exit(f"Snapshot file is empty: {path}")
    print(f"(source: JSONL {path}, {len(snaps)} snapshots)")
    return snaps


def parse_snap_ts(ts):
    """Parse '2026-05-20 10:00:12 IST' -> tz-aware datetime."""
    s = ts.replace(" IST", "")
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=IST)


def parse_time_str(hhmm, date_str):
    h, m = map(int, hhmm.split(":"))
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.replace(hour=h, minute=m, tzinfo=IST)


def nearest_snap(snaps, target_time):
    return min(snaps, key=lambda s: abs((parse_snap_ts(s["timestamp"]) - target_time).total_seconds()))


def evaluate(entry_snap, check_snap, symbol):
    e = entry_snap["data"][symbol]
    c = check_snap["data"][symbol]
    sig = e["signal"]
    entry_price = e["spot"]["price"]
    check_price = c["spot"]["price"]
    move_pct = (check_price - entry_price) / entry_price * 100 if entry_price else 0

    direction = sig["signal"]
    t1, t2, sl = sig.get("target1", 0), sig.get("target2", 0), sig.get("stop_loss", 0)

    verdict = "NEUTRAL (no trade)"
    correct = None
    if direction == "CALL":
        if t2 and check_price >= t2:
            verdict, correct = "T2 HIT (full win)", True
        elif t1 and check_price >= t1:
            verdict, correct = "T1 HIT (partial win)", True
        elif sl and check_price <= sl:
            verdict, correct = "SL HIT (loss)", False
        elif check_price > entry_price:
            verdict, correct = "OPEN (direction correct, no target yet)", True
        else:
            verdict, correct = "DRAWDOWN (direction wrong, no SL yet)", False
    elif direction == "PUT":
        if t2 and check_price <= t2:
            verdict, correct = "T2 HIT (full win)", True
        elif t1 and check_price <= t1:
            verdict, correct = "T1 HIT (partial win)", True
        elif sl and check_price >= sl:
            verdict, correct = "SL HIT (loss)", False
        elif check_price < entry_price:
            verdict, correct = "OPEN (direction correct, no target yet)", True
        else:
            verdict, correct = "DRAWDOWN (direction wrong, no SL yet)", False

    return {
        "symbol": symbol,
        "entry_ts": entry_snap["timestamp"],
        "check_ts": check_snap["timestamp"],
        "direction": direction,
        "confidence": sig.get("confidence", 0),
        "entry_price": entry_price,
        "check_price": check_price,
        "move_pct": move_pct,
        "t1": t1, "t2": t2, "sl": sl,
        "verdict": verdict,
        "directionally_correct": correct,
        "reasons": sig.get("reasons", []),
        "evidence_quality": sig.get("evidence_quality", "?"),
    }


def print_single(r):
    print(f"\n=== {r['symbol']} ===")
    print(f"Entry   @ {r['entry_ts']}: spot {r['entry_price']:.2f}")
    print(f"  Signal: {r['direction']} (confidence {r['confidence']}%, evidence {r['evidence_quality']})")
    if r['direction'] != "NEUTRAL":
        print(f"  Targets: T1={r['t1']}  T2={r['t2']}  SL={r['sl']}")
    print(f"Check   @ {r['check_ts']}: spot {r['check_price']:.2f}")
    print(f"  Move: {r['move_pct']:+.2f}%")
    print(f"Verdict: {r['verdict']}")
    if r['direction'] != "NEUTRAL":
        print(f"Directionally correct: {r['directionally_correct']}")
    print("\nSignal reasons:")
    for line in r['reasons']:
        print(f"  • {line}")


def run_single(snaps, args):
    entry_t = parse_time_str(args.entry, args.date)
    if args.check:
        check_t = parse_time_str(args.check, args.date)
    else:
        check_t = entry_t + timedelta(minutes=args.horizon)
    entry_snap = nearest_snap(snaps, entry_t)
    check_snap = nearest_snap(snaps, check_t)
    entry_drift = abs((parse_snap_ts(entry_snap["timestamp"]) - entry_t).total_seconds())
    check_drift = abs((parse_snap_ts(check_snap["timestamp"]) - check_t).total_seconds())
    if entry_drift > 600:
        print(f"WARNING: nearest entry snapshot is {entry_drift:.0f}s away from requested time")
    if check_drift > 600:
        print(f"WARNING: nearest check snapshot is {check_drift:.0f}s away from requested time")
    result = evaluate(entry_snap, check_snap, args.symbol)
    print_single(result)


def run_full_day(snaps, args):
    horizon = timedelta(minutes=args.horizon)
    results = []
    for snap in snaps:
        t = parse_snap_ts(snap["timestamp"])
        future_t = t + horizon
        future = nearest_snap(snaps, future_t)
        future_drift = abs((parse_snap_ts(future["timestamp"]) - future_t).total_seconds())
        if future_drift > 600:
            continue
        if parse_snap_ts(future["timestamp"]) <= t:
            continue  # need a strictly forward snapshot
        r = evaluate(snap, future, args.symbol)
        if r["direction"] == "NEUTRAL":
            continue
        results.append(r)

    if not results:
        print(f"No non-neutral {args.symbol} signals to evaluate in this file.")
        return

    n = len(results)
    correct = sum(1 for r in results if r["directionally_correct"])
    t1_hits = sum(1 for r in results if "T1 HIT" in r["verdict"] or "T2 HIT" in r["verdict"])
    t2_hits = sum(1 for r in results if "T2 HIT" in r["verdict"])
    sl_hits = sum(1 for r in results if "SL HIT" in r["verdict"])

    print(f"\n=== {args.symbol} full-day summary ({args.date}, {args.horizon}-min horizon) ===")
    print(f"Total non-neutral signals: {n}")
    print(f"Directionally correct:     {correct:>3d} / {n}  ({100*correct/n:.1f}%)")
    print(f"Hit T1 or better:          {t1_hits:>3d} / {n}  ({100*t1_hits/n:.1f}%)")
    print(f"Hit T2:                    {t2_hits:>3d} / {n}  ({100*t2_hits/n:.1f}%)")
    print(f"Hit SL:                    {sl_hits:>3d} / {n}  ({100*sl_hits/n:.1f}%)")
    print()
    print(f"{'entry':>5s}  {'->':>2s}  {'check':>5s}  {'dir':>5s}  {'conf':>6s}  {'spot in':>9s} -> {'spot out':>9s}  {'move%':>6s}  verdict")
    for r in results:
        print(
            f"{r['entry_ts'][11:16]}  ->  {r['check_ts'][11:16]}  "
            f"{r['direction']:>5s}  {r['confidence']:>5.1f}%  "
            f"{r['entry_price']:>9.2f} -> {r['check_price']:>9.2f}  "
            f"{r['move_pct']:>+5.2f}%  {r['verdict']}"
        )


def cli():
    p = argparse.ArgumentParser(description="Backtest snapshots from the recorder")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--entry", help="Entry time HH:MM (IST). Omit for full-day summary.")
    p.add_argument("--check", help="Check time HH:MM (IST). Default: entry + horizon.")
    p.add_argument("--symbol", default="NIFTY", choices=["NIFTY", "BANKNIFTY"])
    p.add_argument("--horizon", type=int, default=30, help="Evaluation horizon in minutes (default 30).")
    args = p.parse_args()

    snaps = load_snapshots(args.date, args.symbol)
    print(f"Loaded {len(snaps)} snapshots for {args.date} / {args.symbol}")
    print(f"Span: {snaps[0]['timestamp']}  ->  {snaps[-1]['timestamp']}")

    if args.entry:
        run_single(snaps, args)
    else:
        run_full_day(snaps, args)


if __name__ == "__main__":
    cli()
