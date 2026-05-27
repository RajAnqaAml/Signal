"""Fetch SENSEX historical candles from Yahoo Finance and push to Supabase.

Yahoo Finance limits for ^BSESN (SENSEX):
  - 5m candles:  max ~30 days
  - 1h candles:  max ~1 year (1,742 bars)
  - 1d candles:  max ~1 year (250 bars)

Strategy: fetch 5m for the last 30 days + 1h for the full year.
The 1h bars give the backtest engine enough history for trend/ATR analysis.

Usage:
    python fetch_sensex_history.py                  # fetch all + push to Supabase
    python fetch_sensex_history.py --dry-run         # save to JSON only
    python fetch_sensex_history.py --interval 5m     # 5m only (last 30 days)
    python fetch_sensex_history.py --interval 1h     # 1h only (last year)
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi import requests as cfreq

IST = ZoneInfo("Asia/Kolkata")

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
SENSEX_TICKER = "^BSESN"


def _fetch_yahoo(session, ticker, period1, period2, interval):
    """Fetch candle data from Yahoo Finance. Returns list of dicts."""
    params = {
        "symbol": ticker,
        "period1": str(int(period1)),
        "period2": str(int(period2)),
        "interval": interval,
        "includePrePost": "false",
        "events": "",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    resp = session.get(YAHOO_CHART_URL.format(ticker=ticker),
                       params=params, headers=headers, timeout=30)
    if resp.status_code != 200:
        return [], resp.status_code

    data = resp.json()
    result = data.get("chart", {}).get("result")
    if not result:
        return [], 0

    res = result[0]
    timestamps = res.get("timestamp") or []
    quote = res.get("indicators", {}).get("quote", [{}])[0]
    n = len(timestamps)

    candles = []
    for i, ts in enumerate(timestamps):
        o = quote.get("open", [None] * n)[i]
        h = quote.get("high", [None] * n)[i]
        l = quote.get("low", [None] * n)[i]
        c = quote.get("close", [None] * n)[i]
        v = (quote.get("volume") or [0] * n)[i] or 0
        if c is None:
            continue
        candles.append({
            "ts": ts,
            "open": round(o, 2) if o else None,
            "high": round(h, 2) if h else None,
            "low": round(l, 2) if l else None,
            "close": round(c, 2),
            "volume": v,
        })
    return candles, 200


def fetch_candles(interval="5m", days=None):
    """Fetch SENSEX candles at the given interval."""
    session = cfreq.Session(impersonate="chrome")
    # Warm Yahoo cookies
    session.get("https://finance.yahoo.com/", timeout=10)

    now = datetime.now(tz=IST)

    if days is None:
        if interval == "5m":
            days = 29
        elif interval in ("15m", "1h"):
            days = 365
        else:
            days = 365

    # For 5m data, Yahoo caps at ~30 days so no chunking needed
    if interval == "5m" and days <= 60:
        start = now - timedelta(days=days)
        print(f"Fetching {interval} candles for last {days} days ...", end=" ", flush=True)
        candles, status = _fetch_yahoo(session, SENSEX_TICKER,
                                       start.timestamp(), now.timestamp(), interval)
        print(f"{len(candles)} candles (HTTP {status})")
        return candles

    # For longer periods, chunk into 59-day windows
    chunk_days = 59 if interval in ("5m", "15m") else 180
    start = now - timedelta(days=days)
    all_candles = []
    chunk_start = start

    while chunk_start < now:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), now)
        label = f"{chunk_start.strftime('%Y-%m-%d')} to {chunk_end.strftime('%Y-%m-%d')}"
        print(f"  {label} ...", end=" ", flush=True)

        candles, status = _fetch_yahoo(session, SENSEX_TICKER,
                                       chunk_start.timestamp(), chunk_end.timestamp(),
                                       interval)
        if status != 200:
            print(f"HTTP {status} (skipped)")
        else:
            print(f"{len(candles)} candles")
        all_candles.extend(candles)

        chunk_start = chunk_end
        if chunk_start < now:
            time.sleep(1)

    # Deduplicate
    seen = set()
    unique = []
    for c in all_candles:
        if c["ts"] not in seen:
            seen.add(c["ts"])
            unique.append(c)
    unique.sort(key=lambda x: x["ts"])
    return unique


def save_json(candles, path, interval):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"candles": candles, "symbol": "SENSEX", "interval": interval}, f)
    size_kb = os.path.getsize(path) / 1024
    print(f"Saved to {path} ({len(candles)} candles, {size_kb:.0f} KB)")


def push_to_supabase(candles, interval_minutes):
    try:
        import db
    except ImportError:
        print("ERROR: db module not found")
        return
    if not db.is_configured():
        print("ERROR: Supabase not configured. Set SUPABASE_URL + SUPABASE_SERVICE_KEY in .env")
        return
    print(f"Pushing {len(candles)} candles ({interval_minutes}m) to Supabase ...")
    result = db.insert_candle_batch("SENSEX", candles, interval=interval_minutes, source="yahoo")
    inserted = result.get("inserted", 0)
    submitted = result.get("submitted", 0)
    print(f"  Inserted: {inserted}, Submitted: {submitted}, Skipped: {submitted - inserted}")


INTERVAL_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "1d": 1440}


def main():
    parser = argparse.ArgumentParser(description="Fetch SENSEX history from Yahoo Finance")
    parser.add_argument("--interval", choices=["5m", "1h", "1d", "all"], default="all",
                        help="Candle interval (default: all = 5m + 1h)")
    parser.add_argument("--days", type=int, default=None, help="Override days to fetch")
    parser.add_argument("--dry-run", action="store_true", help="Save JSON only, no Supabase")
    args = parser.parse_args()

    intervals = ["5m", "1h"] if args.interval == "all" else [args.interval]

    for interval in intervals:
        days = args.days or (29 if interval == "5m" else 365)
        print(f"\n{'='*50}")
        print(f"SENSEX {interval} candles — last {days} days")
        print(f"{'='*50}")

        candles = fetch_candles(interval=interval, days=days)
        if not candles:
            print("No candles fetched.")
            continue

        first = datetime.fromtimestamp(candles[0]["ts"], tz=IST)
        last = datetime.fromtimestamp(candles[-1]["ts"], tz=IST)
        print(f"Total: {len(candles)} candles | {first.strftime('%Y-%m-%d')} to {last.strftime('%Y-%m-%d')}")

        json_path = f"history/SENSEX_{interval}_{days}d.json"
        save_json(candles, json_path, interval)

        if not args.dry_run:
            push_to_supabase(candles, INTERVAL_MINUTES[interval])

    print("\nDone!")


if __name__ == "__main__":
    main()
