"""Deep-dive into working NSE endpoints for OI/derivative data."""
from curl_cffi import requests as cfreq
import time, json

s = cfreq.Session(impersonate="chrome")
s.get("https://www.nseindia.com", timeout=10)
time.sleep(1)

# 1. Variations - has NIFTY / BANKNIFTY keys
print("=" * 60)
print("VARIATIONS: NIFTY section")
print("=" * 60)
r = s.get("https://www.nseindia.com/api/live-analysis-variations?index=gainers",
          timeout=10, headers={"Referer": "https://www.nseindia.com"})
data = r.json()
nifty_data = data.get("NIFTY", [])
print(f"NIFTY items: {len(nifty_data)}")
if nifty_data:
    print(f"Keys: {list(nifty_data[0].keys())}")
    for item in nifty_data[:3]:
        print(json.dumps(item, indent=2)[:300])
        print("---")

bnifty_data = data.get("BANKNIFTY", [])
print(f"\nBANKNIFTY items: {len(bnifty_data)}")
if bnifty_data:
    print(f"Keys: {list(bnifty_data[0].keys())}")
    print(json.dumps(bnifty_data[0], indent=2)[:300])

time.sleep(1)

# 2. OI Spurts - deeper look at nested structure
print("\n" + "=" * 60)
print("OI SPURTS: structure")
print("=" * 60)
r = s.get("https://www.nseindia.com/api/live-analysis-oi-spurts-contracts",
          timeout=10, headers={"Referer": "https://www.nseindia.com"})
data = r.json()
items = data.get("data", [])
for item in items:
    for key, val in item.items():
        if isinstance(val, list):
            print(f"\nCategory: '{key}' -> {len(val)} items")
            if val:
                print(f"  Keys: {list(val[0].keys())}")
                # Find any NIFTY index entries
                nifty = [v for v in val if "NIFTY" in str(v)]
                print(f"  NIFTY-related: {len(nifty)}")
                if nifty:
                    print(f"  Example: {json.dumps(nifty[0], indent=2)[:300]}")
                elif val:
                    print(f"  First: {json.dumps(val[0], indent=2)[:300]}")

time.sleep(1)

# 3. Try more derivative endpoints
print("\n" + "=" * 60)
print("ADDITIONAL ENDPOINTS")
print("=" * 60)
extra = [
    "/api/live-analysis-oi-spurts-underlying",
    "/api/live-analysis-most-active-underlying",
    "/api/live-analysis-volume-gainers",
]
for ep in extra:
    try:
        r = s.get(f"https://www.nseindia.com{ep}", timeout=10,
                  headers={"Referer": "https://www.nseindia.com"})
        sz = len(r.content)
        preview = r.text[:120].replace("\n", " ") if sz > 5 else r.text
        print(f"{ep:50s} -> {r.status_code} | {sz:>6d}b | {preview}")
    except Exception as e:
        print(f"{ep:50s} -> ERROR: {e}")
    time.sleep(0.5)
