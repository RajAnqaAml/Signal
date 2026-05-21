"""Dump OI spurts structure to understand the data format."""
from curl_cffi import requests as cfreq
import json, time

s = cfreq.Session(impersonate="chrome")
s.get("https://www.nseindia.com", timeout=10)
time.sleep(1)

# OI Spurts
r = s.get("https://www.nseindia.com/api/live-analysis-oi-spurts-contracts",
          timeout=10, headers={"Referer": "https://www.nseindia.com"})
d = r.json()
items = d.get("data", [])

for block in items:
    for cat_name, contracts in block.items():
        if not isinstance(contracts, list):
            continue
        symbols = set(c.get("symbol","") for c in contracts)
        nifty_count = sum(1 for c in contracts if c.get("symbol") == "NIFTY")
        bn_count = sum(1 for c in contracts if c.get("symbol") == "BANKNIFTY")
        types = set(c.get("type","") for c in contracts)
        print(f"Category: {cat_name}")
        print(f"  Total: {len(contracts)}, Types: {types}")
        print(f"  NIFTY: {nifty_count}, BANKNIFTY: {bn_count}")
        print(f"  All symbols: {symbols}")
        if contracts:
            c = contracts[0]
            print(f"  Sample: symbol={c.get('symbol')}, type={c.get('type')}, pChange={c.get('pChange')}, changeInOI={c.get('changeInOI')}, optionType={c.get('optionType')}")
        print()

# Also check allIndices for dayHigh/dayLow
time.sleep(1)
r2 = s.get("https://www.nseindia.com/api/allIndices",
           timeout=10, headers={"Referer": "https://www.nseindia.com"})
d2 = r2.json()
for idx in d2.get("data", []):
    if idx.get("index") in ("NIFTY 50", "NIFTY BANK"):
        print(f"\n{idx['index']}: last={idx.get('last')}, dayHigh={idx.get('dayHigh')}, dayLow={idx.get('dayLow')}, open={idx.get('open')}, prevClose={idx.get('previousClose')}, pctChange={idx.get('percentChange')}")
