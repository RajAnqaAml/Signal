"""One-shot backfill: pushes local JSONL snapshots + history/*.json into Supabase.

Run locally with service key in .env:
    python migrate_to_supabase.py

Idempotent: re-running is safe (UNIQUE constraints + ignore_duplicates).

Reports:
    - snapshots inserted per file
    - candles inserted per symbol
    - any rows skipped due to duplicate (ts, symbol) keys
"""
import json
import os
import sys
from glob import glob

import db


def migrate_snapshots():
    files = sorted(glob("snapshots/*.jsonl"))
    if not files:
        print("(no snapshots/*.jsonl files to migrate)")
        return 0
    print(f"=== Snapshots: {len(files)} files ===")
    total_inserted = 0
    total_submitted = 0
    for path in files:
        n_lines = 0
        n_inserted = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                n_lines += 1
                payload = json.loads(line)
                try:
                    result = db.insert_snapshot(payload)
                    n_inserted += result.get("inserted", 0)
                    total_submitted += result.get("submitted", 0)
                except Exception as e:
                    print(f"  ! error in {path} line {n_lines}: {e}")
        print(f"  {path}: {n_lines} lines, {n_inserted} rows inserted (skipped: {(n_lines*2) - n_inserted})")
        total_inserted += n_inserted
    print(f"Snapshots total: {total_inserted} rows inserted (submitted: {total_submitted})")
    return total_inserted


def migrate_candles():
    history_files = [
        ("NIFTY", "history/NIFTY_5m_30d.json"),
        ("BANKNIFTY", "history/BANKNIFTY_5m_30d.json"),
        ("VIX", "history/VIX_5m_30d.json"),
    ]
    print(f"\n=== Historical candles ===")
    total_inserted = 0
    for symbol, path in history_files:
        if not os.path.exists(path):
            print(f"  {path}: missing, skipping")
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # Two formats: our NIFTY/BANKNIFTY have {"candles": [...]}, Yahoo VIX has {"chart": {...}}
        if "candles" in data:
            candles = data["candles"]
        else:
            res = data["chart"]["result"][0]
            ts_list = res.get("timestamp", [])
            q = res["indicators"]["quote"][0]
            candles = []
            for i, ts in enumerate(ts_list):
                if q["close"][i] is None:
                    continue
                candles.append({
                    "ts": ts,
                    "open": q["open"][i],
                    "high": q["high"][i],
                    "low": q["low"][i],
                    "close": q["close"][i],
                    "volume": (q.get("volume") or [0]*len(ts_list))[i] or 0,
                })
        result = db.insert_candle_batch(symbol, candles)
        n = result.get("inserted", 0)
        submitted = result.get("submitted", 0)
        print(f"  {symbol}: {submitted} candles, {n} inserted (skipped {submitted - n} duplicates)")
        total_inserted += n
    print(f"Candles total: {total_inserted}")
    return total_inserted


def main():
    if not db.is_configured():
        print("ERROR: Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env.")
        sys.exit(1)
    print(f"Migrating to: {os.environ['SUPABASE_URL']}")
    print()
    snaps = migrate_snapshots()
    candles = migrate_candles()
    print(f"\nDone. {snaps} snapshot rows + {candles} candle rows total.")


if __name__ == "__main__":
    main()
