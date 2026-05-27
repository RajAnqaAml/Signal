"""Fetch and push 5m candles for ALL symbols. Run after market close.

Usage:
    python fetch_all_history.py              # fetch last 29 days for all
    python fetch_all_history.py --days 5     # just last 5 days (daily refresh)
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from curl_cffi import requests as cfreq

IST = ZoneInfo("Asia/Kolkata")

TICKERS = {
    "SENSEX": "^BSESN",
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
}


def fetch_5m(session, ticker, days):
    now = datetime.now(tz=IST)
    start = now - timedelta(days=days)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {
        "period1": str(int(start.timestamp())),
        "period2": str(int(now.timestamp())),
        "interval": "5m",
        "includePrePost": "false",
    }
    resp = session.get(url, params=params,
                       headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if resp.status_code != 200:
        return []
    data = resp.json()
    result = data.get("chart", {}).get("result", [])
    if not result or not result[0].get("timestamp"):
        return []
    ts_list = result[0]["timestamp"]
    quote = result[0]["indicators"]["quote"][0]
    n = len(ts_list)
    candles = []
    for i, ts in enumerate(ts_list):
        c = quote.get("close", [None]*n)[i]
        if c is None:
            continue
        candles.append({
            "ts": ts,
            "open": round(quote["open"][i], 2) if quote["open"][i] else None,
            "high": round(quote["high"][i], 2) if quote["high"][i] else None,
            "low": round(quote["low"][i], 2) if quote["low"][i] else None,
            "close": round(c, 2),
            "volume": (quote.get("volume") or [0]*n)[i] or 0,
        })
    return candles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=29, help="Days to fetch (default 29)")
    args = parser.parse_args()

    session = cfreq.Session(impersonate="chrome")
    session.get("https://finance.yahoo.com/", timeout=10)

    for symbol, ticker in TICKERS.items():
        print(f"\n{symbol} ({ticker})...")
        candles = fetch_5m(session, ticker, args.days)
        if not candles:
            print(f"  No data")
            continue

        first = datetime.fromtimestamp(candles[0]["ts"], tz=IST)
        last = datetime.fromtimestamp(candles[-1]["ts"], tz=IST)
        print(f"  {len(candles)} candles | {first.strftime('%Y-%m-%d')} to {last.strftime('%Y-%m-%d')}")

        # Save JSON backup
        path = f"history/{symbol}_5m_{args.days}d.json"
        os.makedirs("history", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"candles": candles, "symbol": symbol, "interval": "5m"}, f)
        print(f"  Saved {path}")

        # Push to Supabase
        try:
            import db
            if db.is_configured():
                result = db.insert_candle_batch(symbol, candles, interval=5, source="yahoo")
                ins = result.get("inserted", 0)
                sub = result.get("submitted", 0)
                print(f"  Supabase: {ins} new / {sub} total")
        except Exception as e:
            print(f"  Supabase error: {e}")

        time.sleep(1)

    print("\nDone!")


if __name__ == "__main__":
    main()
