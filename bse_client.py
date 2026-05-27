"""BSE data client for SENSEX option chain and spot data.

Uses BSE's Angular API endpoints (discovered from bseindia.com's appConfig.json):
  - BseIndiaAPI/api/ddlExpiry_IV/w      → expiry dates + strike prices
  - BseIndiaAPI/api/DerivOptionChain_IV/w → full option chain with IV
  - RealTimeBseIndiaAPI/api/GetSensexData/w → SENSEX spot price

The output of fetch_option_chain() is normalized to match NSE's
/api/option-chain-indices JSON structure so that analyze_option_chain()
in app.py works unchanged.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi import requests as cfreq

IST = ZoneInfo("Asia/Kolkata")

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_REALTIME = "https://api.bseindia.com/RealTimeBseIndiaAPI/api"
SENSEX_SCRIP_CD = "1"


class BSEClient:
    """BSE data client using curl_cffi (Chrome TLS impersonation)."""

    CACHE_SECONDS = 20

    def __init__(self):
        self._session = cfreq.Session(impersonate="chrome")
        self._cache = {}
        print("[BSE] Client initialized (curl_cffi)")

    def _headers(self):
        return {
            "Referer": "https://www.bseindia.com/",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.bseindia.com",
        }

    def _fetch_json(self, url, params=None, cache_key=None, cache_ttl=None):
        now = datetime.now()
        ttl = cache_ttl if cache_ttl is not None else self.CACHE_SECONDS
        if cache_key and cache_key in self._cache:
            cached_data, cached_at = self._cache[cache_key]
            if (now - cached_at).total_seconds() < ttl:
                return cached_data

        try:
            resp = self._session.get(
                url, params=params, headers=self._headers(), timeout=15,
            )
            if resp.status_code == 200 and len(resp.content) > 10:
                text = resp.text.strip()
                if text[:1] in ("{", "["):
                    data = resp.json()
                    if cache_key:
                        self._cache[cache_key] = (data, now)
                    return data
                print(f"[BSE] {url}: got HTML instead of JSON ({len(text)} bytes)")
        except Exception as e:
            print(f"[BSE] {url} error: {e}")

        if cache_key and cache_key in self._cache:
            return self._cache[cache_key][0]
        return None

    # ── SENSEX Spot Price ──────────────────────────────────────────────

    def fetch_spot_price(self):
        """Fetch SENSEX spot price from BSE's real-time API."""
        data = self._fetch_json(
            f"{BSE_REALTIME}/GetSensexData/w",
            cache_key="sensex_spot",
            cache_ttl=10,
        )
        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        row = data[0]

        def parse_num(val):
            if not val or val == "-":
                return 0.0
            return float(str(val).replace(",", ""))

        price = parse_num(row.get("ltp"))
        return {
            "price": round(price, 2),
            "change": round(parse_num(row.get("perchg")), 2),
            "open": round(parse_num(row.get("I_open")), 2),
            "high": round(parse_num(row.get("High")), 2),
            "low": round(parse_num(row.get("Low")), 2),
            "prev_close": round(parse_num(row.get("Prev_Close")), 2),
        }

    # ── Expiry Dates ──────────────────────────────────────────────────

    def _fetch_expiry_dates(self):
        """Get available SENSEX expiry dates from BSE.
        Returns list of expiry date strings like ['27 May 2026', '04 Jun 2026', ...].
        """
        data = self._fetch_json(
            f"{BSE_API}/ddlExpiry_IV/w",
            params={"ProductType": "IO", "scrip_cd": SENSEX_SCRIP_CD},
            cache_key="sensex_expiries",
            cache_ttl=300,
        )
        if not data or "Table1" not in data:
            return []
        return [row["ExpiryDate"] for row in data["Table1"] if "ExpiryDate" in row]

    # ── Option Chain ──────────────────────────────────────────────────

    def fetch_option_chain(self):
        """Fetch SENSEX option chain and normalize to NSE-compatible format.

        Returns a dict matching NSE's /api/option-chain-indices structure:
        {
            "records": {
                "expiryDates": [...],
                "timestamp": "...",
                "data": [
                    {"expiryDate": "...", "strikePrice": N,
                     "CE": {"openInterest": N, "changeinOpenInterest": N, "impliedVolatility": N},
                     "PE": {"openInterest": N, "changeinOpenInterest": N, "impliedVolatility": N}},
                ]
            }
        }
        """
        expiries = self._fetch_expiry_dates()
        if not expiries:
            print("[BSE] No expiry dates available")
            return None

        nearest_expiry = expiries[0]

        raw = self._fetch_json(
            f"{BSE_API}/DerivOptionChain_IV/w",
            params={
                "Expiry": nearest_expiry,
                "scrip_cd": SENSEX_SCRIP_CD,
                "strprice": "0",
            },
            cache_key=f"sensex_oc_{nearest_expiry}",
        )
        if not raw or "Table" not in raw:
            print("[BSE] Option chain data missing or malformed")
            return None

        rows = raw["Table"]
        if not rows:
            return None

        def parse_num(val):
            if not val or val == "" or val == "-":
                return 0
            try:
                return float(str(val).replace(",", ""))
            except (ValueError, TypeError):
                return 0

        # Build NSE-compatible records
        nse_rows = []
        for row in rows:
            strike_raw = parse_num(row.get("Strike_Price"))
            if strike_raw <= 0:
                continue

            strike = int(round(strike_raw))

            nse_row = {
                "expiryDate": nearest_expiry,
                "strikePrice": strike,
                "CE": {
                    "openInterest": int(parse_num(row.get("C_Open_Interest"))),
                    "changeinOpenInterest": int(parse_num(row.get("C_Absolute_Change_OI"))),
                    "impliedVolatility": parse_num(row.get("C_IV")),
                    "lastPrice": parse_num(row.get("C_Last_Trd_Price")),
                    "volume": int(parse_num(row.get("C_Vol_Traded"))),
                },
                "PE": {
                    "openInterest": int(parse_num(row.get("Open_Interest"))),
                    "changeinOpenInterest": int(parse_num(row.get("Absolute_Change_OI"))),
                    "impliedVolatility": parse_num(row.get("IV")),
                    "lastPrice": parse_num(row.get("Last_Trd_Price")),
                    "volume": int(parse_num(row.get("Vol_Traded"))),
                },
            }
            nse_rows.append(nse_row)

        if not nse_rows:
            return None

        # Convert BSE expiry format "27 May 2026" → NSE format "27-May-2026"
        nse_expiry_dates = []
        for exp in expiries:
            try:
                dt = datetime.strptime(exp, "%d %b %Y")
                nse_expiry_dates.append(dt.strftime("%d-%b-%Y"))
            except ValueError:
                nse_expiry_dates.append(exp)

        nearest_nse_fmt = nse_expiry_dates[0] if nse_expiry_dates else nearest_expiry

        for r in nse_rows:
            r["expiryDate"] = nearest_nse_fmt

        # Build timestamp from ASON field or current time
        timestamp = datetime.now(tz=IST).strftime("%d-%b-%Y %H:%M:%S")
        ason = raw.get("ASON")
        if isinstance(ason, dict) and ason.get("DT_TM"):
            timestamp = ason["DT_TM"]
        elif isinstance(ason, list) and ason and ason[0].get("DT_TM"):
            timestamp = ason[0]["DT_TM"]

        return {
            "records": {
                "expiryDates": nse_expiry_dates,
                "timestamp": timestamp,
                "data": nse_rows,
            }
        }
