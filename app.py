import json
import math
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from curl_cffi import requests as cfreq
from flask import Flask, jsonify, render_template
from flask_cors import CORS

IST = ZoneInfo("Asia/Kolkata")

try:
    from bse_client import BSEClient
    bse = BSEClient()
except Exception:
    bse = None


def is_market_open(now=None):
    """NSE cash/F&O session: Mon-Fri 09:15-15:30 IST.
    Holiday calendar is not handled here — /api/marketStatus is the authoritative
    source for that and is consulted at request time when the clock window matches.

    We extend the close to 15:35 IST so a cron fire that lands a minute or two
    after the bell still captures the closing snapshot. NSE's option-chain and
    spot endpoints serve the close-of-day values for several minutes after 15:30.
    """
    now = now or datetime.now(tz=IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=35, second=0, microsecond=0)
    return open_t <= now <= close_t

app = Flask(__name__)
CORS(app)


# ─── NSE Connection Manager ────────────────────────────────────────────────
class NSEClient:
    """NSE data client using curl_cffi (Chrome TLS impersonation).
    Fetches from endpoints that work without Akamai JS challenge:
      - allIndices -> VIX, spot prices, index data
      - oiSpurts  -> OI change data for F&O contracts
      - equityStockIndices -> market breadth (advances/declines)
    """

    BASE_URL = "https://www.nseindia.com"
    COOKIE_EXPIRY_MINUTES = 3
    CACHE_SECONDS = 10

    def __init__(self):
        self._session = cfreq.Session(impersonate="chrome")
        self._cookies_at = None
        self._cache = {}  # key -> (data, timestamp)
        print("[NSE] Client initialized (curl_cffi)")

    # ── Session & Cookie Management ────────────────────────────────────
    def _ensure_cookies(self):
        """Prime session cookies by visiting NSE homepage."""
        now = datetime.now()
        if (
            self._cookies_at
            and (now - self._cookies_at) < timedelta(minutes=self.COOKIE_EXPIRY_MINUTES)
            and len(self._session.cookies) > 0
        ):
            return True
        try:
            r = self._session.get(self.BASE_URL, timeout=10)
            cookie_count = len(self._session.cookies)
            print(f"[NSE] Cookie prime -> {r.status_code} | {cookie_count} cookies")
            if cookie_count > 0:
                self._cookies_at = now
                return True
        except Exception as e:
            print(f"[NSE] Cookie prime error: {e}")
        return False

    def _fetch_json(self, path, referer=None, cache_key=None, cache_ttl=None):
        """Fetch JSON from an NSE API path with caching."""
        now = datetime.now()
        ttl = cache_ttl if cache_ttl is not None else self.CACHE_SECONDS
        if cache_key and cache_key in self._cache:
            cached_data, cached_at = self._cache[cache_key]
            if (now - cached_at).total_seconds() < ttl:
                return cached_data

        self._ensure_cookies()
        url = f"{self.BASE_URL}{path}"
        headers = {}
        if referer:
            headers["Referer"] = referer
        try:
            resp = self._session.get(url, timeout=15, headers=headers)
            if resp.status_code == 200 and len(resp.content) > 10:
                data = resp.json()
                if cache_key:
                    self._cache[cache_key] = (data, now)
                return data
            print(f"[NSE] {path}: {resp.status_code} | {len(resp.content)}b")
        except Exception as e:
            print(f"[NSE] {path} error: {e}")

        # Return stale cache if available
        if cache_key and cache_key in self._cache:
            return self._cache[cache_key][0]
        return None

    # ── Data Fetchers ──────────────────────────────────────────────────
    def fetch_all_indices(self):
        """Fetch allIndices (VIX, spot prices, all index data)."""
        return self._fetch_json(
            "/api/allIndices",
            referer=f"{self.BASE_URL}/market-data/live-market-indices",
            cache_key="allIndices",
        )

    def fetch_vix(self):
        """Extract India VIX from allIndices."""
        data = self.fetch_all_indices()
        if data:
            for idx in data.get("data", []):
                if idx.get("indexSymbol") == "INDIA VIX":
                    return {
                        "value": round(idx.get("last", 0), 2),
                        "change": round(idx.get("percentChange", 0), 2),
                    }
        return {"value": 0, "change": 0}

    def fetch_spot_price(self, symbol="NIFTY"):
        """Extract spot price from allIndices."""
        data = self.fetch_all_indices()
        target = "NIFTY 50" if symbol == "NIFTY" else "NIFTY BANK"
        if data:
            for idx in data.get("data", []):
                if idx.get("index") == target:
                    price = idx.get("last") or 0
                    open_p = idx.get("open") or price
                    prev_close = idx.get("previousClose") or price
                    high = idx.get("dayHigh")
                    low = idx.get("dayLow")
                    # If dayHigh/dayLow are None, estimate from price action
                    if not high or not low:
                        swing = max(abs(price - open_p), price * 0.005)
                        high = max(price, open_p) + swing * 0.3
                        low = min(price, open_p) - swing * 0.3
                    return {
                        "price": round(price, 2),
                        "change": round(idx.get("percentChange") or 0, 2),
                        "open": round(open_p, 2),
                        "high": round(high, 2),
                        "low": round(low, 2),
                        "prev_close": round(prev_close, 2),
                    }
        return {"price": 0, "change": 0, "open": 0, "high": 0, "low": 0, "prev_close": 0}

    def fetch_oi_data(self):
        """Fetch OI spurts data — contracts with significant OI changes."""
        return self._fetch_json(
            "/api/live-analysis-oi-spurts-contracts",
            referer=f"{self.BASE_URL}/market-data/live-analysis/oi-spurts",
            cache_key="oiSpurts",
        )

    def fetch_market_breadth(self):
        """Fetch market breadth (advances/declines) from allIndices."""
        data = self.fetch_all_indices()
        if data:
            return {
                "advances": int(data.get("advances", 0)),
                "declines": int(data.get("declines", 0)),
                "unchanged": int(data.get("unchanged", 0)),
            }
        return {"advances": 0, "declines": 0, "unchanged": 0}

    def fetch_variations(self):
        """Fetch NIFTY/BANKNIFTY derivative variations data."""
        return self._fetch_json(
            "/api/live-analysis-variations?index=gainers",
            referer=f"{self.BASE_URL}/market-data/live-analysis/price-volume-variation",
            cache_key="variations",
        )

    def fetch_intraday_chart(self, symbol="NIFTY"):
        """Fetch intraday tick data for an index.
        Returns the raw {"grapthData": [[unix_ms, price], ...]} payload or None.
        """
        index_name = "NIFTY 50" if symbol == "NIFTY" else "NIFTY BANK"
        encoded = index_name.replace(" ", "%20")
        return self._fetch_json(
            f"/api/chart-databyindex?index={encoded}&indices=true",
            referer=f"{self.BASE_URL}/market-data/live-equity-market",
            cache_key=f"chart_{symbol}",
            cache_ttl=30,
        )

    def fetch_option_chain(self, symbol="NIFTY"):
        """Fetch full option chain via /api/option-chain-v3 (works for indices).
        Two-step: get nearest expiry from contract-info, then fetch OC.
        SENSEX trades on BSE so returns None gracefully.
        """
        # SENSEX is a BSE product — NSE has no index OC for it
        if symbol == "SENSEX":
            return None

        # Step 1: nearest expiry (cached 1h — expiry dates don't change intraday)
        ci = self._fetch_json(
            f"/api/option-chain-contract-info?symbol={symbol}",
            referer=f"{self.BASE_URL}/option-chain",
            cache_key=f"ocContractInfo_{symbol}",
            cache_ttl=3600,
        )
        expiry = (ci or {}).get("expiryDates", [None])[0]

        # Step 2: fetch OC for nearest expiry
        ep = f"/api/option-chain-v3?type=Indices&symbol={symbol}"
        if expiry:
            ep += f"&expiry={urllib.parse.quote(expiry)}"
        data = self._fetch_json(
            ep,
            referer=f"{self.BASE_URL}/option-chain",
            cache_key=f"optionChain_{symbol}",
            cache_ttl=20,
        )
        if not data or not data.get("records"):
            return None
        return data

    def fetch_market_status(self):
        """Fetch NSE market-status (authoritative for trading holidays)."""
        return self._fetch_json(
            "/api/marketStatus",
            referer=f"{self.BASE_URL}/market-data/pre-open-market-cm-and-emerge-market",
            cache_key="marketStatus",
            cache_ttl=3600,
        )

    def fetch_derivative_indices(self):
        """Fetch indices eligible in derivatives from allIndices."""
        data = self.fetch_all_indices()
        result = {}
        if data:
            for idx in data.get("data", []):
                sym = idx.get("indexSymbol", "")
                if sym in ("NIFTY 50", "NIFTY BANK"):
                    key = "NIFTY" if sym == "NIFTY 50" else "BANKNIFTY"
                    price = idx.get("last") or 0
                    open_p = idx.get("open") or price
                    high = idx.get("dayHigh")
                    low = idx.get("dayLow")
                    if not high or not low:
                        swing = max(abs(price - open_p), price * 0.005)
                        high = max(price, open_p) + swing * 0.3
                        low = min(price, open_p) - swing * 0.3
                    result[key] = {
                        "price": round(price, 2),
                        "change": round(idx.get("percentChange") or 0, 2),
                        "open": round(open_p, 2),
                        "high": round(high, 2),
                        "low": round(low, 2),
                        "prev_close": round(idx.get("previousClose") or 0, 2),
                        "year_high": round(idx.get("yearHigh") or 0, 2),
                        "year_low": round(idx.get("yearLow") or 0, 2),
                    }
        return result


# Singleton NSE client
nse = NSEClient()


# ─── Technical Indicators ───────────────────────────────────────────────────
def compute_rsi(prices, period=14):
    """Compute RSI from a price series."""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_ema(prices, period):
    """Compute EMA for given period."""
    if len(prices) < period:
        return prices[-1] if len(prices) > 0 else 0
    multiplier = 2 / (period + 1)
    ema = np.mean(prices[:period])
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return round(ema, 2)


def compute_macd(prices):
    """Compute MACD line, signal line, histogram."""
    if len(prices) < 26:
        return {"macd": 0, "signal": 0, "histogram": 0}
    ema12 = compute_ema(prices, 12)
    ema26 = compute_ema(prices, 26)
    macd_line = round(ema12 - ema26, 2)
    # Approximate signal from full series
    macd_series = []
    m = 2 / 13
    ema12_val = np.mean(prices[:12])
    ema26_val = np.mean(prices[:26])
    for i in range(26, len(prices)):
        ema12_val = (prices[i] - ema12_val) * (2 / 13) + ema12_val
        ema26_val = (prices[i] - ema26_val) * (2 / 27) + ema26_val
        macd_series.append(ema12_val - ema26_val)
    if len(macd_series) >= 9:
        signal = compute_ema(np.array(macd_series), 9)
    else:
        signal = macd_series[-1] if macd_series else 0
    histogram = round(macd_line - signal, 2)
    return {"macd": macd_line, "signal": round(signal, 2), "histogram": histogram}


def compute_atr(highs, lows, closes, period=14):
    """Average True Range over the most recent `period` bars.
    Returns None if not enough data — callers should fall back to a static value.
    """
    if len(closes) < period + 1:
        return None
    tr_vals = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_vals.append(tr)
    if len(tr_vals) < period:
        return None
    return float(np.mean(tr_vals[-period:]))


def compute_adx(highs, lows, closes, period=14):
    """Compute ADX, +DI, -DI using Wilder's smoothing.
    Returns dict with adx, plus_di, minus_di, adx_rising, di_spread.
    Returns None if insufficient data.
    """
    n = len(closes)
    if n < period + 2:
        return None

    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)

    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0
        minus_dm[i] = down if (down > up and down > 0) else 0
        tr[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))

    # Wilder's smoothing (EMA with alpha = 1/period)
    atr = np.mean(tr[1:period + 1])
    plus_dm_smooth = np.mean(plus_dm[1:period + 1])
    minus_dm_smooth = np.mean(minus_dm[1:period + 1])

    adx_vals = []
    for i in range(period + 1, n):
        atr = atr - (atr / period) + tr[i]
        plus_dm_smooth = plus_dm_smooth - (plus_dm_smooth / period) + plus_dm[i]
        minus_dm_smooth = minus_dm_smooth - (minus_dm_smooth / period) + minus_dm[i]

        plus_di = (plus_dm_smooth / atr * 100) if atr > 0 else 0
        minus_di = (minus_dm_smooth / atr * 100) if atr > 0 else 0
        di_sum = plus_di + minus_di
        dx = (abs(plus_di - minus_di) / di_sum * 100) if di_sum > 0 else 0
        adx_vals.append(dx)

    if len(adx_vals) < period:
        return None

    # ADX is the smoothed average of DX
    adx = np.mean(adx_vals[:period])
    for dx in adx_vals[period:]:
        adx = (adx * (period - 1) + dx) / period

    # Current +DI and -DI
    cur_plus_di = (plus_dm_smooth / atr * 100) if atr > 0 else 0
    cur_minus_di = (minus_dm_smooth / atr * 100) if atr > 0 else 0

    # ADX 3 bars ago for "rising" check
    adx_prev = adx
    if len(adx_vals) >= period + 3:
        adx_prev = np.mean(adx_vals[:period])
        for dx in adx_vals[period:-3]:
            adx_prev = (adx_prev * (period - 1) + dx) / period

    return {
        "adx": round(adx, 2),
        "plus_di": round(cur_plus_di, 2),
        "minus_di": round(cur_minus_di, 2),
        "adx_rising": adx > adx_prev,
        "di_spread": round(abs(cur_plus_di - cur_minus_di), 2),
    }


def compute_supertrend(highs, lows, closes, period=10, multiplier=3):
    """Simplified SuperTrend indicator."""
    if len(closes) < period:
        return "NEUTRAL"
    atr_vals = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        atr_vals.append(tr)
    if len(atr_vals) < period:
        return "NEUTRAL"
    atr = np.mean(atr_vals[-period:])
    upper_band = ((highs[-1] + lows[-1]) / 2) + multiplier * atr
    lower_band = ((highs[-1] + lows[-1]) / 2) - multiplier * atr
    if closes[-1] > upper_band:
        return "BULLISH"
    elif closes[-1] < lower_band:
        return "BEARISH"
    return "NEUTRAL"


# ─── OI Analysis (from OI Spurts endpoint) ─────────────────────────────────
def analyze_oi_spurts(oi_data, symbol="NIFTY"):
    """Analyze OI spurts data for directional bias on a given symbol.
    NSE pre-classifies contracts by type:
      RR = Rise-in-OI-Rise  -> Long Buildup  (OI up + Price up)
      RS = Rise-in-OI-Slide -> Short Buildup (OI up + Price down)
      SS = Slide-in-OI-Slide -> Long Unwinding (OI down + Price down)
      SR = Slide-in-OI-Rise  -> Short Covering (OI down + Price up)
    """
    if not oi_data or "data" not in oi_data:
        return None

    target_sym = symbol if symbol == "NIFTY" else "BANKNIFTY"
    categories = oi_data.get("data", [])

    long_buildup = 0
    short_buildup = 0
    long_unwinding = 0
    short_covering = 0
    matched_contracts = []

    for cat_block in categories:
        for cat_name, items in cat_block.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if item.get("symbol") != target_sym:
                    continue
                matched_contracts.append(item)
                t = item.get("type", "")
                if t == "RR":
                    long_buildup += 1
                elif t == "RS":
                    short_buildup += 1
                elif t == "SS":
                    long_unwinding += 1
                elif t == "SR":
                    short_covering += 1

    total = long_buildup + short_buildup + long_unwinding + short_covering
    if total == 0:
        bias = "NEUTRAL"
        bias_score = 0
    else:
        bullish = long_buildup + short_covering
        bearish = short_buildup + long_unwinding
        if bullish > bearish * 1.2:
            bias = "BULLISH"
            bias_score = min((bullish - bearish) / max(total, 1) * 5, 3)
        elif bearish > bullish * 1.2:
            bias = "BEARISH"
            bias_score = -min((bearish - bullish) / max(total, 1) * 5, 3)
        else:
            bias = "NEUTRAL"
            bias_score = 0

    return {
        "long_buildup": long_buildup,
        "short_buildup": short_buildup,
        "long_unwinding": long_unwinding,
        "short_covering": short_covering,
        "contract_count": len(matched_contracts),
        "bias": bias,
        "bias_score": round(bias_score, 2),
    }


# ─── Option Chain Analysis ──────────────────────────────────────────────────
OC_STALENESS_MAX_MIN = 5  # If NSE payload is older than this, drop Factor 8.


def _oc_payload_age_min(oc_data, now_ist=None):
    """Return how many minutes old the NSE option-chain payload is, based on
    `records.timestamp` ("23-May-2026 15:23:45"). Returns None if the field is
    missing or unparseable (we silently allow it in that case).
    """
    try:
        ts_str = (oc_data or {}).get("records", {}).get("timestamp")
        if not ts_str:
            return None
        # NSE format: "23-May-2026 15:23:45"
        ts = datetime.strptime(ts_str, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        now_ist = now_ist or datetime.now(tz=IST)
        return (now_ist - ts).total_seconds() / 60
    except (ValueError, TypeError, AttributeError):
        return None


def analyze_option_chain(oc_data, spot, symbol="NIFTY", now_ist=None):
    """Compute PCR, Max Pain, support/resistance strikes, ATM OI flow, IV skew
    from the NSE option-chain payload. Operates on the **nearest expiry only**
    so signals reflect this-week positioning, not far-month noise.

    Returns None if the payload is missing, malformed, OR stale beyond
    OC_STALENESS_MAX_MIN minutes (CDN cache hits return None so Factor 8 is
    zeroed out instead of polluting the score with old data).
    """
    if not oc_data or "records" not in oc_data:
        return None

    # Staleness guard: NSE has known CDN cases where it returns yesterday's
    # data with HTTP 200. Drop the payload if its timestamp is too old.
    age_min = _oc_payload_age_min(oc_data, now_ist)
    if age_min is not None and age_min > OC_STALENESS_MAX_MIN:
        print(
            f"[OC stale] {symbol} payload is {age_min:.1f} min old "
            f"(threshold {OC_STALENESS_MAX_MIN} min) - skipping Factor 8",
            flush=True,
        )
        return None

    records = oc_data["records"]
    rows = records.get("data", [])
    expiries = records.get("expiryDates", [])
    if not rows or not expiries:
        return None

    nearest_expiry = expiries[0]
    step = 50 if symbol == "NIFTY" else 100  # BANKNIFTY and SENSEX both use 100

    # option-chain-v3 uses DD-MM-YYYY in CE/PE sub-objects; old API used DD-Mon-YYYY
    # at row level. Normalise so both formats match.
    try:
        nearest_expiry_alt = datetime.strptime(nearest_expiry, "%d-%b-%Y").strftime("%d-%m-%Y")
    except Exception:
        nearest_expiry_alt = nearest_expiry

    strikes = {}  # strike -> {"ce_oi", "pe_oi", "ce_chg", "pe_chg", "ce_iv", "pe_iv"}
    for row in rows:
        # Row-level expiryDate (old API) or CE/PE-level (new option-chain-v3 API)
        row_expiry = (row.get("expiryDate")
                      or (row.get("CE") or {}).get("expiryDate")
                      or (row.get("PE") or {}).get("expiryDate"))
        if row_expiry and row_expiry not in (nearest_expiry, nearest_expiry_alt):
            continue
        strike = row.get("strikePrice")
        if strike is None:
            continue
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        strikes[strike] = {
            "ce_oi": ce.get("openInterest", 0) or 0,
            "pe_oi": pe.get("openInterest", 0) or 0,
            "ce_chg": ce.get("changeinOpenInterest", 0) or 0,
            "pe_chg": pe.get("changeinOpenInterest", 0) or 0,
            "ce_iv": ce.get("impliedVolatility", 0) or 0,
            "pe_iv": pe.get("impliedVolatility", 0) or 0,
            # Live option premium (LTP) + intraday change — needed for Rs-priced
            # Entry/T1/T2/SL tickets.
            "ce_ltp": ce.get("lastPrice", 0) or 0,
            "pe_ltp": pe.get("lastPrice", 0) or 0,
            "ce_ltp_chg": ce.get("change", 0) or 0,
            "pe_ltp_chg": pe.get("change", 0) or 0,
        }

    if not strikes:
        return None

    total_ce_oi = sum(s["ce_oi"] for s in strikes.values())
    total_pe_oi = sum(s["pe_oi"] for s in strikes.values())
    total_ce_chg = sum(s["ce_chg"] for s in strikes.values())
    total_pe_chg = sum(s["pe_chg"] for s in strikes.values())

    pcr_total = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0
    pcr_change = round(total_pe_chg / total_ce_chg, 2) if total_ce_chg > 0 else 0

    max_ce_strike = max(strikes.items(), key=lambda kv: kv[1]["ce_oi"])[0]
    max_pe_strike = max(strikes.items(), key=lambda kv: kv[1]["pe_oi"])[0]

    # Max pain — strike that minimizes total option writer payout.
    pain = {}
    sorted_strikes = sorted(strikes.keys())
    for k_test in sorted_strikes:
        total_payout = 0
        for k_strike, s in strikes.items():
            total_payout += s["ce_oi"] * max(k_test - k_strike, 0)
            total_payout += s["pe_oi"] * max(k_strike - k_test, 0)
        pain[k_test] = total_payout
    max_pain = min(pain.items(), key=lambda kv: kv[1])[0] if pain else 0

    # ATM ±3 steps: who's adding OI today?
    atm_strike = round(spot / step) * step
    atm_band = [atm_strike + i * step for i in range(-3, 4)]
    atm_ce_chg = sum(strikes.get(k, {}).get("ce_chg", 0) for k in atm_band)
    atm_pe_chg = sum(strikes.get(k, {}).get("pe_chg", 0) for k in atm_band)

    # Live premiums for ATM ±2 strikes — the raw material for Rs-priced tickets.
    premiums = {}
    for k in (atm_strike + i * step for i in range(-2, 3)):
        s = strikes.get(k)
        if not s:
            continue
        premiums[int(k)] = {
            "ce_ltp": round(float(s["ce_ltp"]), 2),
            "pe_ltp": round(float(s["pe_ltp"]), 2),
            "ce_chg": round(float(s["ce_ltp_chg"]), 2),
            "pe_chg": round(float(s["pe_ltp_chg"]), 2),
        }

    # IV skew over ATM ±5, ignoring zero-IV (illiquid) strikes.
    iv_band = [atm_strike + i * step for i in range(-5, 6)]
    ce_ivs = [strikes[k]["ce_iv"] for k in iv_band if k in strikes and strikes[k]["ce_iv"] > 0]
    pe_ivs = [strikes[k]["pe_iv"] for k in iv_band if k in strikes and strikes[k]["pe_iv"] > 0]
    iv_skew = round(np.mean(pe_ivs) - np.mean(ce_ivs), 2) if ce_ivs and pe_ivs else 0

    # Determine if THIS contract expires today. NSE publishes expiryDates[0]
    # as the nearest expiry; we just parse it. Format is "DD-MMM-YYYY"
    # (e.g. "26-May-2026"). Handles weekly + monthly expiries + holiday
    # shifts automatically — no hardcoded calendar required.
    is_expiry_today = False
    expiry_iso = None
    try:
        expiry_dt = datetime.strptime(nearest_expiry, "%d-%b-%Y").date()
        expiry_iso = expiry_dt.isoformat()
        today_ist = (now_ist or datetime.now(tz=IST)).date()
        is_expiry_today = (expiry_dt == today_ist)
    except (ValueError, TypeError):
        # Unparseable expiry string -- leave is_expiry_today False; the
        # _classify_v3_tier function will fall back to the weekday() check.
        pass

    return {
        "expiry": nearest_expiry,
        "expiry_iso": expiry_iso,
        "is_expiry_today": is_expiry_today,
        "pcr_total": pcr_total,
        "pcr_change": pcr_change,
        "max_pain": max_pain,
        "max_pain_distance_pct": round((max_pain - spot) / spot * 100, 2) if spot > 0 else 0,
        "max_ce_oi_strike": max_ce_strike,
        "max_pe_oi_strike": max_pe_strike,
        "atm_ce_change": int(atm_ce_chg),
        "atm_pe_change": int(atm_pe_chg),
        "iv_skew": iv_skew,
        "total_ce_oi": int(total_ce_oi),
        "total_pe_oi": int(total_pe_oi),
        # Live option premiums (LTP) for ATM ±2 strikes — for Rs-priced tickets.
        "atm_strike": int(atm_strike),
        "premiums": premiums,
    }


# ─── Real Intraday History ──────────────────────────────────────────────────
def build_real_history(chart_data, bucket_minutes=5):
    """Resample NSE intraday tick data into N-minute OHLC bars.
    Returns (highs, lows, closes) as numpy arrays. Empty arrays if insufficient data.
    """
    if not chart_data or "grapthData" not in chart_data:
        return np.array([]), np.array([]), np.array([])
    ticks = chart_data.get("grapthData") or []
    if len(ticks) < 30:
        return np.array([]), np.array([]), np.array([])

    arr = np.array(ticks, dtype=np.float64)  # columns: [ts_ms, price]
    ts_ms = arr[:, 0]
    prices = arr[:, 1]

    bucket_ms = bucket_minutes * 60 * 1000
    bucket_idx = (ts_ms // bucket_ms).astype(np.int64)

    highs, lows, closes = [], [], []
    current_bucket = bucket_idx[0]
    bucket_prices = [prices[0]]
    for i in range(1, len(prices)):
        if bucket_idx[i] != current_bucket:
            highs.append(max(bucket_prices))
            lows.append(min(bucket_prices))
            closes.append(bucket_prices[-1])
            current_bucket = bucket_idx[i]
            bucket_prices = []
        bucket_prices.append(prices[i])
    if bucket_prices:
        highs.append(max(bucket_prices))
        lows.append(min(bucket_prices))
        closes.append(bucket_prices[-1])

    return np.array(highs), np.array(lows), np.array(closes)


def compute_orb(highs, lows, closes, now_ist=None, orb_bars=12):
    """Compute Opening Range Breakout data from intraday OHLC bars.

    Returns dict with or_high, or_low, broke_above, broke_below, orb_signal.
    orb_bars=12 means first 60 minutes (12 x 5-min bars).
    Returns None if not enough data or still within the opening range period.

    orb_signal:
      "CALL"    — price broke above OR high only (bullish breakout)
      "PUT"     — price broke below OR low only (bearish breakout)
      "CHOPPY"  — price broke both sides (whipsaw day, no trade)
      "INSIDE"  — price still within OR range (no conviction yet)
      None      — still in opening range period (< orb_bars)
    """
    if len(highs) < orb_bars + 1:
        return None

    or_high = float(np.max(highs[:orb_bars]))
    or_low = float(np.min(lows[:orb_bars]))

    post_or_highs = highs[orb_bars:]
    post_or_lows = lows[orb_bars:]

    if len(post_or_highs) == 0:
        return {"or_high": or_high, "or_low": or_low,
                "broke_above": False, "broke_below": False, "orb_signal": None}

    broke_above = bool(np.any(post_or_highs > or_high))
    broke_below = bool(np.any(post_or_lows < or_low))

    if broke_above and broke_below:
        orb_signal = "CHOPPY"
    elif broke_above:
        orb_signal = "CALL"
    elif broke_below:
        orb_signal = "PUT"
    else:
        orb_signal = "INSIDE"

    return {
        "or_high": round(or_high, 2),
        "or_low": round(or_low, 2),
        "broke_above": broke_above,
        "broke_below": broke_below,
        "orb_signal": orb_signal,
    }


# ─── Signal Generator ───────────────────────────────────────────────────────
def _gap_decay_weight(now_ist):
    """Gap factor decay: step function so contributions stay integer-clean.
    Returns 1.0 / 0.5 / 0 based on minutes since market open.

    Why a step function:
      - Previous design used a linear decay (1.0 -> 0.0 over 180 min), which
        injected fractional values into trend_score. That broke threshold
        comparisons like `score <= -3` for scores that were actually -2.95
        due to a -0.5 gap contribution. Three Thursday 2026-05-21 snaps
        displayed -3 but returned NEUTRAL because of this.
      - Steps keep trend_score on the integer grid the rest of the engine
        assumes.
    """
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    minutes_since_open = (now_ist - market_open).total_seconds() / 60
    if minutes_since_open <= 75:    # 09:15 - 10:30 IST: gap fully in play
        return 1.0
    if minutes_since_open <= 135:   # 10:30 - 11:30 IST: half weight
        return 0.5
    return 0.0                       # 11:30 onwards: gap is priced in


def _gap_bucket(gap_pct):
    """Convert a gap percentage into an integer score contribution.
    Returns +2 / +1 / 0 / -1 / -2 — never a fractional value.
    """
    if gap_pct >= 1.0:
        return 2
    if gap_pct >= 0.3:
        return 1
    if gap_pct <= -1.0:
        return -2
    if gap_pct <= -0.3:
        return -1
    return 0


def generate_signal(spot_data, vix_data, oi_analysis, breadth, technicals,
                    oc_analysis=None, history_source="synthetic", symbol="NIFTY",
                    now_ist=None):
    """
    Multi-factor signal engine.
    Combines: VIX, price action, OI buildup, option chain flow, market breadth,
    and (when real history is available) technical indicators.
    """
    if not spot_data or spot_data["price"] == 0:
        return {
            "signal": "NEUTRAL", "confidence": 0,
            "reasons": ["Insufficient data"],
            "entry": 0, "target1": 0, "target2": 0, "stop_loss": 0,
            "evidence_quality": "flow-only",
        }
    now_ist = now_ist or datetime.now(tz=IST)
    gap_weight = _gap_decay_weight(now_ist)

    spot = spot_data["price"]
    change_pct = spot_data["change"]
    day_open = spot_data["open"]
    day_high = spot_data["high"]
    day_low = spot_data["low"]
    prev_close = spot_data["prev_close"]
    vix = vix_data["value"]
    vix_change = vix_data["change"]

    score = 0  # positive -> CALL, negative -> PUT
    trend_score = 0  # track price-action contribution separately for contrarian check
    oi_score = 0     # track OI bias contribution separately
    reasons = []

    # ── Factor 1: Intraday Move (change since OPEN, not from prev close) ──
    # Using change-from-open avoids double-counting the gap (which Factor 3 handles).
    # SENSEX is ~20% less volatile in % terms than NIFTY (0.89% avg range vs 1.24%),
    # so it uses lower thresholds to fire at equivalent conviction levels.
    MOVE_STRONG = {"NIFTY": 0.8, "BANKNIFTY": 0.8, "SENSEX": 0.65}
    MOVE_MILD = {"NIFTY": 0.3, "BANKNIFTY": 0.3, "SENSEX": 0.25}
    move_strong = MOVE_STRONG.get(symbol, 0.8)
    move_mild = MOVE_MILD.get(symbol, 0.3)

    intraday_pct = ((spot - day_open) / day_open * 100) if day_open > 0 else 0
    if intraday_pct > move_strong:
        trend_score += 2
        reasons.append(f"Strong intraday up-move +{intraday_pct:.2f}% from open -> Bullish momentum")
    elif intraday_pct > move_mild:
        trend_score += 1
        reasons.append(f"Positive intraday move +{intraday_pct:.2f}% from open")
    elif intraday_pct < -move_strong:
        trend_score -= 2
        reasons.append(f"Strong intraday down-move {intraday_pct:.2f}% from open -> Bearish momentum")
    elif intraday_pct < -move_mild:
        trend_score -= 1
        reasons.append(f"Negative intraday move {intraday_pct:.2f}% from open")
    else:
        reasons.append(f"Flat from open ({intraday_pct:+.2f}%, change_from_prev_close {change_pct}%)")

    # ── Factor 2: Price vs Day Range ──
    day_range = day_high - day_low if day_high > day_low else 1
    range_pos = (spot - day_low) / day_range
    if range_pos > 0.8:
        trend_score += 1
        reasons.append("Trading near day high -> Buyers in control")
    elif range_pos < 0.2:
        trend_score -= 1
        reasons.append("Trading near day low -> Sellers in control")

    # ── Factor 3: Gap Analysis (Open vs Prev Close) with step-function decay ──
    # Gap matters at 09:15 but is fully priced in by 11:30. _gap_decay_weight
    # returns 1.0 / 0.5 / 0, and _gap_bucket returns an integer ±1/±2 — together
    # they keep the contribution integer-clean.
    gap_pct = ((day_open - prev_close) / prev_close * 100) if prev_close is not None and prev_close > 0 else 0
    gap_bucket = _gap_bucket(gap_pct)
    if gap_bucket != 0 and gap_weight > 0:
        # int(round(...)) guarantees an integer even when weight is 0.5 and
        # bucket is odd (e.g. 0.5 * 1 -> 0; 0.5 * 2 -> 1).
        contribution = int(round(gap_bucket * gap_weight))
        trend_score += contribution
        if contribution != 0:
            direction = "up" if gap_pct > 0 else "down"
            reasons.append(
                f"Gap-{direction} {gap_pct:+.2f}% (bucket {gap_bucket:+d}, "
                f"decay {gap_weight:.1f}) -> {contribution:+d}"
            )

    score += trend_score

    # ── Factor 4: VIX Analysis ──
    if vix < 14:
        score += 1
        reasons.append(f"VIX {vix} -> Low fear, trend continuation likely")
    elif vix > 22:
        score -= 1
        reasons.append(f"VIX {vix} -> Elevated fear, caution advised")
    else:
        reasons.append(f"VIX {vix} -> Normal range")

    if vix_change < -5:
        score += 1
        reasons.append(f"VIX falling {vix_change}% -> Fear subsiding (Bullish)")
    elif vix_change > 5:
        score -= 1
        reasons.append(f"VIX rising +{vix_change}% -> Fear increasing (Bearish)")

    # ── Factor 5: OI Buildup Bias ──
    if oi_analysis:
        oi_bias = oi_analysis.get("bias_score", 0)
        if oi_bias > 1:
            oi_score += 2
            reasons.append(f"OI Spurts: Long buildup dominant -> Bullish ({oi_analysis['long_buildup']}L / {oi_analysis['short_buildup']}S)")
        elif oi_bias > 0:
            oi_score += 1
            reasons.append(f"OI Spurts: Mildly bullish bias")
        elif oi_bias < -1:
            oi_score -= 2
            reasons.append(f"OI Spurts: Short buildup dominant -> Bearish ({oi_analysis['short_buildup']}S / {oi_analysis['long_buildup']}L)")
        elif oi_bias < 0:
            oi_score -= 1
            reasons.append(f"OI Spurts: Mildly bearish bias")
        else:
            reasons.append("OI Spurts: Neutral")
    score += oi_score

    # ── Factor 6: Market Breadth ──
    if breadth and breadth["advances"] + breadth["declines"] > 0:
        adv = breadth["advances"]
        dec = breadth["declines"]
        breadth_ratio = adv / max(adv + dec, 1)
        if breadth_ratio > 0.7:
            score += 1
            reasons.append(f"Broad rally: {adv}A / {dec}D -> Strong market breadth")
        elif breadth_ratio < 0.3:
            score -= 1
            reasons.append(f"Broad selloff: {adv}A / {dec}D -> Weak market breadth")

    # ── Factor 7: Technical Indicators (only when on real intraday history) ──
    if history_source == "real":
        rsi = technicals.get("rsi", 50)
        macd_hist = technicals.get("macd", {}).get("histogram", 0)
        ema9 = technicals.get("ema9", 0)
        ema21 = technicals.get("ema21", 0)
        supertrend = technicals.get("supertrend", "NEUTRAL")

        if rsi > 65:
            score += 1
            reasons.append(f"RSI {rsi} -> Bullish momentum")
        elif rsi < 35:
            score -= 1
            reasons.append(f"RSI {rsi} -> Bearish momentum")

        if macd_hist > 0:
            score += 1
            reasons.append("MACD histogram positive -> Bullish cross")
        elif macd_hist < 0:
            score -= 1
            reasons.append("MACD histogram negative -> Bearish cross")

        if ema9 > ema21 and ema9 > 0:
            score += 1
            reasons.append(f"EMA9 ({ema9}) > EMA21 ({ema21}) -> Bullish trend")
        elif ema21 > ema9 and ema21 > 0:
            score -= 1
            reasons.append(f"EMA9 ({ema9}) < EMA21 ({ema21}) -> Bearish trend")

        if supertrend == "BULLISH":
            score += 1
            reasons.append("SuperTrend -> Bullish")
        elif supertrend == "BEARISH":
            score -= 1
            reasons.append("SuperTrend -> Bearish")
    else:
        reasons.append("Real intraday history unavailable — indicators omitted")

    # ── Factor 8: Option Chain Flow (PCR, Max Pain, strike-wise OI, IV skew) ──
    if oc_analysis:
        pcr = oc_analysis.get("pcr_total", 0)
        pcr_chg = oc_analysis.get("pcr_change", 0)
        max_ce = oc_analysis.get("max_ce_oi_strike", 0)
        max_pe = oc_analysis.get("max_pe_oi_strike", 0)
        atm_ce_chg = oc_analysis.get("atm_ce_change", 0)
        atm_pe_chg = oc_analysis.get("atm_pe_change", 0)
        iv_skew = oc_analysis.get("iv_skew", 0)

        # Overall PCR (heaviest weight)
        if pcr > 1.3 and pcr_chg > 1:
            score += 2
            reasons.append(f"PCR {pcr} + put writing on top (Δ {pcr_chg}) -> strong support")
        elif pcr < 0.7 and pcr_chg < 1:
            score -= 2
            reasons.append(f"PCR {pcr} + call writing on top (Δ {pcr_chg}) -> strong resistance")
        elif pcr > 1.2:
            score += 1
            reasons.append(f"PCR {pcr} -> put writers dominant (bullish)")
        elif pcr < 0.8:
            score -= 1
            reasons.append(f"PCR {pcr} -> call writers dominant (bearish)")

        # Spot vs key OI strikes
        if max_pe > 0 and 0 < (spot - max_pe) / max_pe < 0.003:
            score += 1
            reasons.append(f"Spot just above max PE OI ({max_pe}) -> support test")
        if max_ce > 0 and 0 < (max_ce - spot) / max_ce < 0.003:
            score -= 1
            reasons.append(f"Spot just below max CE OI ({max_ce}) -> resistance test")

        # ATM OI flow
        if atm_pe_chg > abs(atm_ce_chg) * 1.5 and atm_pe_chg > 0:
            score += 1
            reasons.append("ATM put writing > call writing -> support forming")
        elif atm_ce_chg > abs(atm_pe_chg) * 1.5 and atm_ce_chg > 0:
            score -= 1
            reasons.append("ATM call writing > put writing -> resistance forming")

        # IV skew
        if iv_skew > 2:
            score -= 1
            reasons.append(f"IV skew {iv_skew} -> put premium fear (bearish)")
        elif iv_skew < -2:
            score += 1
            reasons.append(f"IV skew {iv_skew} -> call premium chase (bullish)")

    # ── Contrarian filter: when OI Spurts opposes the price trend direction ──
    # Catches the "exhausted move" case where price says continue but flow says reverse.
    # Applied as a confidence multiplier; can force NEUTRAL if conflict is sharp.
    contrarian_penalty = 1.0
    if trend_score != 0 and oi_score != 0 and (trend_score * oi_score) < 0:
        contrarian_penalty = 0.5
        reasons.append(
            f"⚠ Contrarian: trend score {trend_score:+.2f} vs OI score {oi_score:+.2f} -> confidence halved"
        )
        # If the conflict is strong (price trend ≥ 2 magnitude AND opposing OI score ≥ 1),
        # the engine has no high-conviction direction — refuse to trade.
        if abs(trend_score) >= 2 and abs(oi_score) >= 1:
            reasons.append("Sharp price/OI conflict — forcing NEUTRAL (no trade)")
            return {
                "signal": "NEUTRAL",
                "confidence": 0,
                "score": round(score, 2),
                "trend_score": round(trend_score, 2),
                "oi_score": round(oi_score, 2),
                "gap_weight": round(gap_weight, 2),
                "reasons": reasons,
                "entry": round(spot, 2),
                "target1": 0, "target2": 0, "stop_loss": 0,
                "evidence_quality": "full" if history_source == "real" else "flow-only",
            }

    # ── Determine Signal (|score| ≥ 3 required; weak tier removed) ──
    confidence = min(abs(score) * 12, 95) * contrarian_penalty
    step = 50 if symbol == "NIFTY" else 100

    # ── Volatility-scaled targets (ATR-based) ──
    # Previous rule used fixed +/- 75 / 60 points which worked on high-vol days
    # but was unreachable on flat days (Friday 2026-05-22: 0 targets hit anywhere).
    # New rule: target = max(static_floor, ATR * multiplier). On a flat day, target
    # shrinks. On a volatile day, target expands to capture larger moves.
    #
    # Symbol-aware static floors -- BANKNIFTY spot is ~2.3x NIFTY and naturally
    # more volatile, so a 50-pt floor (NIFTY-appropriate) would give nonsensically
    # tight BN targets (~0.09% of spot). NIFTY values are unchanged from the prior
    # design.
    # T1/T2 from ATR (or floor). SL is ALWAYS T1/2 to enforce R:R 2:1.
    # Backtest 20d SENSEX: R:R 2:1 swung net P&L from -80 pts to +611 pts.
    # SL floor removed — the R:R 2:1 constraint takes precedence.
    ATR_FLOORS = {
        # symbol: (t1_floor, t2_floor)
        "NIFTY":     (50,  100),
        "BANKNIFTY": (100, 200),
        "SENSEX":    (280, 560),
    }
    t1_floor, t2_floor = ATR_FLOORS.get(symbol, (50, 100))
    atr_val = (technicals or {}).get("atr")
    if atr_val and atr_val > 0:
        t1_pts = max(t1_floor, int(round(atr_val * 1.5)))
        t2_pts = max(t2_floor, int(round(atr_val * 3.0)))
        target_basis = f"ATR={atr_val:.1f}"
    else:
        # Fallback to step-based rule when ATR not available (warmup, no history).
        t1_pts = int(step * 1.5)
        t2_pts = int(step * 3.0)
        target_basis = f"static step={step}"
    # R:R per symbol: SENSEX/BANKNIFTY use 2:1 (SL = T1/2),
    # NIFTY uses 1.67:1 (SL = T1 * 0.6) because NIFTY 5m bar noise (~15-25pts)
    # would trigger 25pt SL too often. Backtest validated.
    SL_RATIOS = {"NIFTY": 0.6, "BANKNIFTY": 0.5, "SENSEX": 0.5}
    sl_pts = int(t1_pts * SL_RATIOS.get(symbol, 0.5))

    def _round_to_step(price):
        return round(price / step) * step

    SCORE_THRESHOLD = 4  # raised from 3: score-3 signals had ~50% SL rate across all symbols
    if score >= SCORE_THRESHOLD:
        signal = "CALL"
        entry = spot
        target1 = _round_to_step(spot + t1_pts)
        target2 = _round_to_step(spot + t2_pts)
        stop_loss = _round_to_step(spot - sl_pts)
        reasons.append(f"Targets: T1={target1} T2={target2} SL={stop_loss} ({target_basis})")
    elif score <= -SCORE_THRESHOLD:
        signal = "PUT"
        entry = spot
        target1 = _round_to_step(spot - t1_pts)
        target2 = _round_to_step(spot - t2_pts)
        stop_loss = _round_to_step(spot + sl_pts)
        reasons.append(f"Targets: T1={target1} T2={target2} SL={stop_loss} ({target_basis})")
    else:
        signal = "NEUTRAL"
        confidence = 0
        entry = spot
        target1 = 0
        target2 = 0
        stop_loss = 0
        reasons.append(f"Score {score:+.2f} below threshold (|4| required) — no trade")

    # ── V3 GATES: classify signal into push_tier ──────────────────────────
    # tier classifies WHAT KIND of signal this is:
    #   "TIER_1" -> high conviction, push to phone, real-money tradeable
    #   "TIER_2" -> moderate, dashboard WATCH only, no phone push
    #   "TIER_3" -> sub-threshold OR refused by V3 gates, no action
    # The gate list explains why a signal didn't make Tier 1 if applicable.
    push_tier, tier_blocks = _classify_v3_tier(
        signal, score, confidence, oi_score, trend_score, contrarian_penalty,
        spot, spot_data, symbol, now_ist, reasons,
        oc_analysis=oc_analysis, technicals=technicals,
    )

    return {
        "signal": signal,
        "confidence": round(confidence, 1),
        "score": round(score, 2),
        "trend_score": round(trend_score, 2),
        "oi_score": round(oi_score, 2),
        "gap_weight": round(gap_weight, 2),
        "reasons": reasons,
        "entry": round(entry, 2),
        "target1": target1,
        "target2": target2,
        "stop_loss": stop_loss,
        "evidence_quality": "full" if history_source == "real" else "flow-only",
        "push_tier": push_tier,        # "TIER_1" / "TIER_2" / "TIER_3"
        "tier_blocks": tier_blocks,    # list of V3 gates the signal failed
    }


# ─── V3 Tier classification (separates "auto-push" from "watch only") ──────
# V3.1 Option A tuned values (validated against 5-day live data):
# Originally 0.30/0.40 refused all 5 winning trades. Looser values catch
# 5 of 5 winners; today's losers still blocked by G8 (expiry day rule).
LATE_PCT_OPEN     = {"NIFTY": 0.60, "BANKNIFTY": 0.70, "SENSEX": 0.60}
LATE_PCT_EXTREME  = {"NIFTY": 0.10, "BANKNIFTY": 0.10, "SENSEX": 0.10}
EXPIRY_DOW        = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 3}  # Tue NSE, Thu BSE


def _classify_v3_tier(signal, score, conf, oi_score, trend_score, contrarian_penalty,
                       spot, spot_data, symbol, now_ist, reasons, oc_analysis=None,
                       technicals=None):
    """Run V3 gates to classify a fired signal.
    Returns ("TIER_1" | "TIER_2" | "TIER_3", list_of_block_reasons).

    Tier 1 (auto-push):  GREEN tier + all gates pass.
    Tier 2 (watch only): non-NEUTRAL signal that failed one+ Tier-1 gates.
    Tier 3 (suppressed): NEUTRAL OR signals that failed Gate 1 quality bar.

    oc_analysis: optional option-chain analysis dict (from analyze_option_chain).
    technicals: optional dict with ADX/DI data under key "adx_di".
    """
    blocks = []
    if signal == "NEUTRAL":
        return "TIER_3", ["signal=NEUTRAL"]

    direction = signal
    now_ist = now_ist or datetime.now(tz=IST)

    # Gate 1 (V3.1 Option A - lenient): require basic score + no contrarian.
    # Previous strict version (score>=4, conf>=48, |oi|>=2) refused every signal
    # in 5-day live data, including the biggest winner (Mon BN CALL @ 09:15 had
    # OI=0 at market open). Dropping |oi|>=1 to catch fresh opening signals.
    abs_score = abs(score)
    if abs_score < 4:
        blocks.append(f"G1: score<4 (got {score:.1f})")
    if contrarian_penalty < 1.0:
        blocks.append("G1: contrarian penalty applied")

    # Gate 4: Late-entry filter (avoid buying tops / selling bottoms).
    # V3.1 skip rules: before 09:30 IST (opening signals) OR when day's range
    # is still small (< 0.15%) -- can't be "late" without a meaningful move yet.
    day_open = spot_data.get("open") or spot
    day_high = spot_data.get("high") or spot
    day_low = spot_data.get("low") or spot
    if day_open > 0:
        range_pct = (day_high - day_low) / day_open * 100
        pct_from_open = (spot - day_open) / day_open * 100
        pct_below_high = (day_high - spot) / day_open * 100
        pct_above_low = (spot - day_low) / day_open * 100
        late_open = LATE_PCT_OPEN.get(symbol, 0.30)
        late_extreme = LATE_PCT_EXTREME.get(symbol, 0.10)
        skip_g4 = (now_ist.hour == 9 and now_ist.minute < 30) or range_pct < 0.15
        if not skip_g4:
            if direction == "CALL":
                if pct_from_open > late_open:
                    blocks.append(f"G4: CALL too late ({pct_from_open:.2f}% > {late_open}% above open)")
                elif pct_below_high < late_extreme and pct_below_high < pct_above_low:
                    blocks.append(f"G4: CALL too close to day high ({pct_below_high:.2f}%)")
            else:  # PUT
                if pct_from_open < -late_open:
                    blocks.append(f"G4: PUT too late ({pct_from_open:.2f}% < -{late_open}% below open)")
                elif pct_above_low < late_extreme and pct_above_low < pct_below_high:
                    blocks.append(f"G4: PUT too close to day low ({pct_above_low:.2f}%)")

    # Gate 11: Trend must confirm signal direction.
    # Prevents pure-OI-driven fires where price isn't actually moving in signal dir.
    # Wed 2026-05-27: BN fired CALL all day (OI=+2, VIX=+1 -> score=+3) but
    # trend_score was 0 -- price wasn't rallying. BN closed RED (-45 from open).
    # Two V3 pushes would have lost Rs 3,398. This gate: no CALL unless trend >= +1,
    # no PUT unless trend <= -1.
    if direction == "CALL" and trend_score < 1:
        blocks.append(f"G11: CALL but trend not confirming (trend={trend_score:+.1f})")
    elif direction == "PUT" and trend_score > -1:
        blocks.append(f"G11: PUT but trend not confirming (trend={trend_score:+.1f})")

    # Gate 10: ADX Gatekeeper — trend strength filter.
    # ADX < 18: no trend, hard block (ranging market = whipsaw zone)
    # ADX 18-23: caution zone, require rising ADX + DI spread > 5
    # ADX > 23: trend confirmed, pass freely
    # ADX > 45: exhaustion, block new entries
    # DI alignment: CALL needs +DI > -DI, PUT needs -DI > +DI
    adx_data = (technicals or {}).get("adx_di")
    if adx_data and isinstance(adx_data, dict):
        adx_val = adx_data.get("adx", 0)
        plus_di = adx_data.get("plus_di", 0)
        minus_di = adx_data.get("minus_di", 0)
        adx_rising = adx_data.get("adx_rising", False)
        di_spread = adx_data.get("di_spread", 0)

        if adx_val < 18:
            blocks.append(f"G10: ADX={adx_val:.1f} < 18 (no trend, ranging market)")
        elif adx_val < 23:
            if not adx_rising or di_spread < 5:
                blocks.append(
                    f"G10: ADX={adx_val:.1f} caution zone "
                    f"(rising={adx_rising}, DI spread={di_spread:.1f})"
                )
        if adx_val > 45:
            blocks.append(f"G10: ADX={adx_val:.1f} > 45 (trend exhaustion)")

        # DI alignment: signal direction must match DI dominance
        if direction == "CALL" and minus_di > plus_di:
            blocks.append(
                f"G10: CALL but -DI({minus_di:.1f}) > +DI({plus_di:.1f}) — bearish DI"
            )
        elif direction == "PUT" and plus_di > minus_di:
            blocks.append(
                f"G10: PUT but +DI({plus_di:.1f}) > -DI({minus_di:.1f}) — bullish DI"
            )

    # Gate 7: Time-of-day filter (after-hours block only; pre-09:30 opening signals allowed).
    if (now_ist.hour, now_ist.minute) >= (14, 30):
        blocks.append("G7: after 14:30 IST (late session)")

    # Gate 8: Expiry day extra strictness.
    # Primary source: NSE's option chain expiryDates[0] (via oc_analysis).
    # Fallback: weekday() check if option chain is unavailable. The NSE source
    # is authoritative -- handles holiday shifts, monthly expiry, per-symbol
    # differences automatically. The fallback is a safety net for OC outages.
    is_expiry_today = None
    if oc_analysis is not None:
        is_expiry_today = oc_analysis.get("is_expiry_today")
    if is_expiry_today is None:
        # OC unavailable (or expiry parse failed); fall back to hardcoded weekday
        is_expiry_today = (now_ist.weekday() == EXPIRY_DOW.get(symbol, 1))
    if is_expiry_today:
        expiry_label = (oc_analysis or {}).get("expiry") or "weekday-fallback"
        if abs_score < 5:
            blocks.append(f"G8: expiry day {expiry_label}, need |score|>=5 (got {abs_score:.1f})")
        if now_ist.hour >= 13:
            blocks.append(f"G8: expiry day {expiry_label}, no fires after 13:00 IST")

    # Classify: no blocks -> TIER_1; G1-only -> TIER_2; critical block -> TIER_3
    if not blocks:
        return "TIER_1", []

    critical_gates = {"G4", "G7", "G8", "G10", "G11"}
    has_critical = any(any(g in b for g in critical_gates) for b in blocks)
    if has_critical:
        return "TIER_3", blocks
    return "TIER_2", blocks


# ─── Synthetic Price History ────────────────────────────────────────────────
def build_price_history(spot_data):
    """Build a synthetic intraday-like price series from spot data for indicator calc.
    Creates a 50-candle series that interpolates from prev_close -> open -> spot
    with realistic noise so RSI/MACD/SuperTrend give meaningful readings.
    """
    if not spot_data or spot_data["price"] == 0:
        return np.array([]), np.array([]), np.array([]), np.array([])
    p = spot_data["price"]
    o = spot_data["open"]
    h = spot_data["high"]
    l = spot_data["low"]
    pc = spot_data["prev_close"]

    n = 50  # number of candles
    np.random.seed(int(abs(p * 100)) % 99991)

    # Phase 1: prev_close -> open (gap, 5 candles representing prior session end)
    # Phase 2: open -> current price (35 candles, main trend with mean-reversion noise)
    # Phase 3: noise around current price (10 candles, recent action)
    day_range = max(h - l, abs(p - o) * 1.5, p * 0.003)
    candle_noise = day_range / n

    closes = []
    # Phase 1: approach open from prev_close
    for i in range(5):
        frac = (i + 1) / 5
        val = pc + (o - pc) * frac + np.random.randn() * candle_noise * 0.5
        closes.append(val)

    # Phase 2: trend from open to near current price
    for i in range(35):
        frac = (i + 1) / 35
        trend = o + (p - o) * frac
        noise = np.random.randn() * candle_noise * (1 + 0.5 * math.sin(i * 0.3))
        closes.append(trend + noise)

    # Phase 3: oscillate near current price
    for i in range(10):
        closes.append(p + np.random.randn() * candle_noise * 0.7)
    closes[-1] = p  # ensure last close is exact spot

    closes = np.array(closes)
    highs = closes + np.abs(np.random.randn(n) * candle_noise * 0.6)
    lows = closes - np.abs(np.random.randn(n) * candle_noise * 0.6)

    return highs, lows, closes, closes


# ─── API Routes ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


def build_signals_payload(now_ist=None, verbose=True):
    """Build the full data+signal dict for NIFTY, BANKNIFTY, and SENSEX.
    Does NOT check market hours — caller is responsible. Returns the dict that
    /api/signals wraps, and that the recorder writes to disk.
    """
    now_ist = now_ist or datetime.now(tz=IST)
    vix_data = nse.fetch_vix()
    oi_data = nse.fetch_oi_data()
    breadth = nse.fetch_market_breadth()
    if verbose:
        print(f"[Signals] VIX: {vix_data} | Breadth: {breadth}")

    results = {}
    for symbol in ["NIFTY", "BANKNIFTY"]:
        spot_data = nse.fetch_spot_price(symbol)
        oi_analysis = analyze_oi_spurts(oi_data, symbol)
        oc_data = nse.fetch_option_chain(symbol)
        oc_analysis = analyze_option_chain(oc_data, spot_data["price"], symbol, now_ist) if oc_data else None

        chart = nse.fetch_intraday_chart(symbol)
        highs, lows, closes = build_real_history(chart)
        if len(closes) >= 30:
            history_source = "real"
            prices = closes
        else:
            history_source = "synthetic"
            highs, lows, closes, prices = build_price_history(spot_data)

        atr_val = compute_atr(highs, lows, closes, period=14) if len(closes) >= 15 else None
        adx_di = compute_adx(highs, lows, closes) if len(closes) >= 30 else None
        orb = compute_orb(highs, lows, closes, now_ist) if len(closes) >= 13 else None
        technicals = {
            "rsi": compute_rsi(prices) if len(prices) > 0 else 50,
            "macd": compute_macd(prices) if len(prices) > 0 else {"macd": 0, "signal": 0, "histogram": 0},
            "ema9": compute_ema(prices, 9) if len(prices) > 0 else 0,
            "ema21": compute_ema(prices, 21) if len(prices) > 0 else 0,
            "supertrend": compute_supertrend(highs, lows, closes) if len(closes) > 0 else "NEUTRAL",
            "atr": atr_val,
            "adx_di": adx_di,
            "orb": orb,
            "history_source": history_source,
            "bars": int(len(closes)),
        }

        signal = generate_signal(
            spot_data, vix_data, oi_analysis, breadth, technicals,
            oc_analysis=oc_analysis, history_source=history_source, symbol=symbol,
            now_ist=now_ist,
        )

        if verbose:
            pcr = oc_analysis["pcr_total"] if oc_analysis else "N/A"
            print(f"[Signals] {symbol} spot={spot_data['price']} signal={signal['signal']}@{signal['confidence']}% PCR={pcr} hist={history_source}")

        results[symbol] = {
            "spot": spot_data,
            "oi": oi_analysis,
            "option_chain": oc_analysis,
            "breadth": breadth,
            "indicators": technicals,
            "signal": signal,
        }

    # ── SENSEX (BSE) — isolated, failure-safe ──
    if bse is not None:
        try:
            symbol = "SENSEX"
            spot_data = bse.fetch_spot_price()
            if spot_data and spot_data["price"] > 0:
                oi_analysis = None  # BSE has no OI spurts endpoint
                oc_data = bse.fetch_option_chain()
                oc_analysis = analyze_option_chain(oc_data, spot_data["price"], symbol, now_ist) if oc_data else None

                history_source = "synthetic"
                highs, lows, closes, prices = build_price_history(spot_data)

                atr_val = compute_atr(highs, lows, closes, period=14) if len(closes) >= 15 else None
                adx_di = compute_adx(highs, lows, closes) if len(closes) >= 30 else None
                orb = compute_orb(highs, lows, closes, now_ist) if len(closes) >= 13 else None
                technicals = {
                    "rsi": compute_rsi(prices) if len(prices) > 0 else 50,
                    "macd": compute_macd(prices) if len(prices) > 0 else {"macd": 0, "signal": 0, "histogram": 0},
                    "ema9": compute_ema(prices, 9) if len(prices) > 0 else 0,
                    "ema21": compute_ema(prices, 21) if len(prices) > 0 else 0,
                    "supertrend": compute_supertrend(highs, lows, closes) if len(closes) > 0 else "NEUTRAL",
                    "atr": atr_val,
                    "adx_di": adx_di,
                    "orb": orb,
                    "history_source": history_source,
                    "bars": int(len(closes)),
                }

                signal = generate_signal(
                    spot_data, vix_data, oi_analysis, breadth, technicals,
                    oc_analysis=oc_analysis, history_source=history_source, symbol=symbol,
                    now_ist=now_ist,
                )

                if verbose:
                    pcr = oc_analysis["pcr_total"] if oc_analysis else "N/A"
                    print(f"[Signals] {symbol} spot={spot_data['price']} signal={signal['signal']}@{signal['confidence']}% PCR={pcr} hist={history_source}")

                results[symbol] = {
                    "spot": spot_data,
                    "oi": oi_analysis,
                    "option_chain": oc_analysis,
                    "breadth": breadth,
                    "indicators": technicals,
                    "signal": signal,
                }
            elif verbose:
                print("[Signals] SENSEX spot unavailable — skipping")
        except Exception as e:
            print(f"[Signals] SENSEX failed (skipping): {e}")
            traceback.print_exc()

    return {
        "timestamp": now_ist.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "vix": vix_data,
        "data": results,
    }


# ─── CPR ───────────────────────────────────────────────────────────────────────

def compute_cpr(symbol: str, now_ist: datetime) -> dict | None:
    """Compute Central Pivot Range from previous trading day's OHLC.

    Returns dict with pivot, TC, BC, width_pct, width_label, position context.
    Returns None if prior-day candles are unavailable.
    """
    import db as _db
    candles = _db.get_candles(symbol, interval=5)
    if not candles:
        return None

    # Group 5-min candles by IST date
    by_day: dict = {}
    for c in candles:
        ts_str = c["ts"].replace("Z", "+00:00") if c["ts"].endswith("Z") else c["ts"]
        try:
            dt  = datetime.fromisoformat(ts_str).astimezone(IST)
            day = dt.date()
        except Exception:
            continue
        if c["high"] is None or c["low"] is None or c["close"] is None:
            continue
        if day not in by_day:
            by_day[day] = {"highs": [], "lows": [], "closes": []}
        by_day[day]["highs"].append(float(c["high"]))
        by_day[day]["lows"].append(float(c["low"]))
        by_day[day]["closes"].append(float(c["close"]))

    today     = now_ist.date()
    prev_days = sorted([d for d in by_day if d < today], reverse=True)
    if not prev_days:
        return None

    prev = prev_days[0]
    d    = by_day[prev]
    if not d["highs"]:
        return None

    prev_high  = max(d["highs"])
    prev_low   = min(d["lows"])
    prev_close = d["closes"][-1]

    pivot     = (prev_high + prev_low + prev_close) / 3
    bc        = (prev_high + prev_low) / 2
    tc        = 2 * pivot - bc          # symmetric around pivot
    width_pct = abs(tc - bc) / pivot * 100

    if width_pct < 0.25:
        width_label = "NARROW"
        day_type    = "Strong TRENDING day expected - high conviction moves likely"
    elif width_pct > 0.45:
        width_label = "WIDE"
        day_type    = "CHOPPY day expected - avoid momentum buys, fade extremes"
    else:
        width_label = "MODERATE"
        day_type    = "Mixed - wait for clear breakout before committing"

    return {
        "pivot":      round(pivot,     1),
        "tc":         round(tc,        1),
        "bc":         round(bc,        1),
        "width_pct":  round(width_pct, 3),
        "width_label":width_label,
        "day_type":   day_type,
        "prev_high":  round(prev_high,  1),
        "prev_low":   round(prev_low,   1),
        "prev_close": round(prev_close, 1),
        "prev_date":  str(prev),
    }


# ─── Signal streak ─────────────────────────────────────────────────────────────

def compute_signal_streak(symbol: str) -> dict:
    """Count consecutive same-direction snapshots from the most recent DB rows.

    Returns {"signal": "PUT"|"CALL"|"WAIT", "count": N, "minutes": N*2}.
    Used to give Gemini memory of sustained trends across 2-min runs.
    """
    import db as _db
    cli = _db.client(service=False) or _db.client(service=True)
    if cli is None:
        return {"signal": "UNKNOWN", "count": 0, "minutes": 0}
    try:
        resp = (
            cli.table("snapshots")
            .select("ts,raw_payload")
            .eq("symbol", symbol)
            .order("ts", desc=True)
            .limit(60)
            .execute()
        )
        rows = resp.data or []
    except Exception:
        return {"signal": "UNKNOWN", "count": 0, "minutes": 0}

    signals = []
    for r in rows:
        raw = r.get("raw_payload") or {}
        sig = raw.get("signal", {}) if isinstance(raw, dict) else {}
        s   = sig.get("signal", "")
        if s in ("CALL", "PUT", "WAIT"):
            signals.append(s)

    if not signals:
        return {"signal": "UNKNOWN", "count": 0, "minutes": 0}

    current = signals[0]
    count   = 0
    for s in signals:
        if s == current:
            count += 1
        else:
            break

    return {"signal": current, "count": count, "minutes": count * 2}


def compute_recent_performance(symbol: str, lookback: int = 20) -> dict:
    """Give the engine memory of its OWN recent results — did my last signals
    win (T1) or lose (SL)? This is the feedback loop the engine was missing.

    Walks back through recent snapshots, finds each TIER_1 entry moment
    (transition into a direction), and resolves it forward to T1/SL using the
    spot prices recorded afterward. Returns a summary the LLM can reason about.

    Returns dict:
        outcomes   : list of recent results, newest first, e.g. ["SL","SL","T1"]
        sl_streak  : consecutive SLs from the most recent entry backward
        whipsaw    : count of direction flips (CALL<->PUT) in the lookback window
        verdict    : short human-readable summary for the prompt
    """
    import db as _db
    cli = _db.client(service=False) or _db.client(service=True)
    if cli is None:
        return {"outcomes": [], "sl_streak": 0, "whipsaw": 0, "verdict": "No history."}
    try:
        resp = (
            cli.table("snapshots")
            .select("ts,raw_payload,spot_price")
            .eq("symbol", symbol)
            .order("ts", desc=True)
            .limit(lookback + 30)   # extra rows to resolve forward outcomes
            .execute()
        )
        rows = list(reversed(resp.data or []))   # chronological (oldest first)
    except Exception:
        return {"outcomes": [], "sl_streak": 0, "whipsaw": 0, "verdict": "No history."}

    if len(rows) < 4:
        return {"outcomes": [], "sl_streak": 0, "whipsaw": 0, "verdict": "Session just started."}

    # Flatten into a clean series
    series = []
    for r in rows:
        raw = r.get("raw_payload") or {}
        sig = raw.get("signal", {}) if isinstance(raw, dict) else {}
        series.append({
            "signal":  sig.get("signal", "?"),
            "tier":    sig.get("push_tier", "?"),
            "spot":    float(r.get("spot_price") or 0),
            "target1": sig.get("target1", 0) or 0,
            "sl":      sig.get("stop_loss", 0) or 0,
        })

    import notify as _notify

    def _resolve(i):
        """Resolve the entry at index i forward → 'T1'/'SL'/'OPEN'."""
        s = series[i]
        d = s["signal"]
        entry = s["spot"]
        if entry <= 0 or d not in ("CALL", "PUT"):
            return None
        t_pts = abs(s["target1"] - entry) if s["target1"] else _notify.TARGET_PTS.get(symbol, 30)
        spts  = abs(s["sl"] - entry)      if s["sl"]      else _notify.SL_PTS.get(symbol, 18)
        t1 = entry - t_pts if d == "PUT" else entry + t_pts
        sl = entry + spts  if d == "PUT" else entry - spts
        for j in range(i + 1, min(i + 1 + 30, len(series))):
            sp = series[j]["spot"]
            if sp <= 0:
                continue
            if d == "PUT":
                if sp <= t1: return "T1"
                if sp >= sl: return "SL"
            else:
                if sp >= t1: return "T1"
                if sp <= sl: return "SL"
        return "OPEN"

    # Identify entry moments and resolve them. An entry = a moment a notification
    # would fire: TIER_1 + direction differs from the immediately previous snapshot
    # (so CALL -> WAIT -> CALL counts as TWO entries, matching real re-entries).
    outcomes = []   # chronological
    prev_sig = None
    for i, s in enumerate(series):
        is_entry = (s["tier"] == "TIER_1" and s["signal"] in ("CALL", "PUT")
                    and s["signal"] != prev_sig)
        if is_entry:
            r = _resolve(i)
            if r in ("T1", "SL"):
                outcomes.append(r)
        prev_sig = s["signal"]   # update on EVERY snapshot incl. WAIT/NEUTRAL

    # SL streak (consecutive SLs from the most recent resolved entry backward)
    sl_streak = 0
    for o in reversed(outcomes):
        if o == "SL":
            sl_streak += 1
        else:
            break

    # Win rate over recent entries — the real "am I being chopped?" signal.
    # In chop, losses interleave with wins (SL,T1,SL,SL), so a low win-rate over
    # the last ~6 entries detects it far better than a pure consecutive streak.
    recent  = outcomes[-6:]
    n_recent = len(recent)
    wins    = sum(1 for o in recent if o == "T1")
    win_rate = round(100 * wins / n_recent) if n_recent else None

    # Whipsaw: direction flips among the last `lookback` directional signals
    recent_dirs = [s["signal"] for s in series[-lookback:] if s["signal"] in ("CALL", "PUT")]
    whipsaw = sum(1 for a, b in zip(recent_dirs, recent_dirs[1:]) if a != b)

    # Verdict — prioritise win-rate (chop detector), then streak, then whipsaw
    newest = outcomes[-5:][::-1]   # newest first, max 5
    if n_recent >= 4 and win_rate is not None and win_rate <= 40:
        verdict = (f"DANGER: only {wins}/{n_recent} recent entries hit target "
                   f"({win_rate}% win-rate). The market is chopping your signals. "
                   f"A tick in your direction is NOT a fresh setup — demand a structural "
                   f"break (new range high/low, CPR level decisively broken) or WAIT.")
    elif sl_streak >= 2:
        verdict = (f"CAUTION: {sl_streak} stop-losses in a row. Be selective — only "
                   f"enter on a clear NEW trigger, not a re-test of the same idea.")
    elif whipsaw >= 5:
        verdict = (f"CHOPPY: {whipsaw} direction flips recently. Range-bound saw — "
                   f"options bleed premium on both sides. Strongly prefer WAIT.")
    elif n_recent >= 3 and win_rate is not None and win_rate >= 70:
        verdict = (f"GOOD: {wins}/{n_recent} recent entries hit target "
                   f"({win_rate}% win-rate). Trend is tradeable — trust clean signals.")
    else:
        verdict = "Mixed/normal — no strong recent pattern."

    return {
        "outcomes":  newest,
        "sl_streak": sl_streak,
        "win_rate":  win_rate,
        "n_recent":  n_recent,
        "whipsaw":   whipsaw,
        "verdict":   verdict,
    }


# ─── AI Engine payload builder ─────────────────────────────────────────────────

def _sanitize(obj):
    """Recursively convert numpy scalars/booleans to native Python types for JSON."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def build_ai_signals_payload(now_ist=None, verbose=True):
    """AI-powered replacement for build_signals_payload().

    Fetches the same market data (spot, OI, option chain, VIX, technicals)
    but calls ai_engine.generate_signal() instead of the rule-based engine.
    Output dict is identical in structure — fully compatible with recorder.py,
    the Supabase DB layer, and the dashboard frontend.
    """
    try:
        import ai_engine as _ai
    except ImportError:
        print("[Signals] ai_engine not found — falling back to rule-based engine", flush=True)
        return build_signals_payload(now_ist=now_ist, verbose=verbose)

    now_ist  = now_ist or datetime.now(tz=IST)
    vix_data = nse.fetch_vix()
    breadth  = nse.fetch_market_breadth()
    if verbose:
        print(f"[AI Signals] VIX: {vix_data} | Breadth: {breadth}")

    results = {}

    # Pre-compute CPR + signal streak for all symbols (DB reads — zero API calls)
    streak_cache = {}
    for _sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
        try:
            streak_cache[_sym] = compute_signal_streak(_sym)
            s = streak_cache[_sym]
            if s["count"] >= 2 and verbose:
                print(f"[AI Signals] {_sym} streak: {s['signal']} x{s['count']} (~{s['minutes']} min)")
        except Exception as _e:
            streak_cache[_sym] = {"signal": "UNKNOWN", "count": 0, "minutes": 0}

    cpr_cache = {}
    for _sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
        try:
            cpr_cache[_sym] = compute_cpr(_sym, now_ist)
            if cpr_cache[_sym] and verbose:
                c = cpr_cache[_sym]
                print(f"[AI Signals] {_sym} CPR: TC={c['tc']} BC={c['bc']} "
                      f"width={c['width_pct']:.2f}% ({c['width_label']})")
        except Exception as _e:
            cpr_cache[_sym] = None
            print(f"[AI Signals] {_sym} CPR failed: {_e}", flush=True)

    # Recent performance — engine's memory of its own T1/SL track record today
    perf_cache = {}
    for _sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
        try:
            perf_cache[_sym] = compute_recent_performance(_sym)
            p = perf_cache[_sym]
            if (p["sl_streak"] >= 2 or p["whipsaw"] >= 5) and verbose:
                print(f"[AI Signals] {_sym} perf: SL-streak={p['sl_streak']} "
                      f"whipsaw={p['whipsaw']} | {p['verdict']}")
        except Exception as _e:
            perf_cache[_sym] = None

    # ── NIFTY and BANKNIFTY (NSE) ──────────────────────────────────────────
    for symbol in ["NIFTY", "BANKNIFTY"]:
        try:
            spot_data   = nse.fetch_spot_price(symbol)
            oc_data     = nse.fetch_option_chain(symbol)
            oc_analysis = analyze_option_chain(oc_data, spot_data["price"], symbol, now_ist) if oc_data else None

            chart = nse.fetch_intraday_chart(symbol)
            highs, lows, closes = build_real_history(chart)
            if len(closes) >= 30:
                history_source = "real"
            else:
                history_source = "synthetic"
                highs, lows, closes, _ = build_price_history(spot_data)

            atr_val = compute_atr(highs, lows, closes, period=14) if len(closes) >= 15 else None
            adx_di  = compute_adx(highs, lows, closes) if len(closes) >= 30 else None

            technicals = {
                "rsi":       compute_rsi(closes)             if len(closes) > 0 else 50,
                "macd":      compute_macd(closes)            if len(closes) > 0 else {},
                "ema9":      compute_ema(closes, 9)          if len(closes) > 0 else 0,
                "ema21":     compute_ema(closes, 21)         if len(closes) > 0 else 0,
                "supertrend":compute_supertrend(highs, lows, closes) if len(closes) > 0 else "NEUTRAL",
                "atr":       atr_val,
                "adx_di":    adx_di,
                "history_source": history_source,
                "bars":      int(len(closes)),
                # Pass raw arrays so AI engine can show the candle table
                "_closes":   closes.tolist() if hasattr(closes, "tolist") else list(closes),
                "_highs":    highs.tolist()  if hasattr(highs,  "tolist") else list(highs),
                "_lows":     lows.tolist()   if hasattr(lows,   "tolist") else list(lows),
            }

            signal = _ai.generate_signal(
                symbol, spot_data, oc_analysis, technicals, vix_data,
                now_ist=now_ist, cpr_data=cpr_cache.get(symbol),
                streak_data=streak_cache.get(symbol),
                perf_data=perf_cache.get(symbol)
            )

            if verbose:
                pcr = oc_analysis["pcr_total"] if oc_analysis else "N/A"
                print(f"[AI Signals] {symbol} spot={spot_data['price']} "
                      f"signal={signal['signal']}@{signal['confidence']:.0f}% "
                      f"tier={signal['push_tier']} PCR={pcr}")

            results[symbol] = {
                "spot": spot_data, "oi": None,
                "option_chain": oc_analysis, "breadth": breadth,
                "indicators": {k: v for k, v in technicals.items() if not k.startswith("_")},
                "signal": signal,
            }
        except Exception as e:
            print(f"[AI Signals] {symbol} failed: {e}", flush=True)
            import traceback; traceback.print_exc()

    # ── SENSEX (BSE) — isolated, failure-safe ─────────────────────────────
    if bse is not None:
        try:
            symbol    = "SENSEX"
            spot_data = bse.fetch_spot_price()
            if spot_data and spot_data["price"] > 0:
                oc_data     = bse.fetch_option_chain()
                oc_analysis = analyze_option_chain(oc_data, spot_data["price"], symbol, now_ist) if oc_data else None

                history_source = "synthetic"
                highs, lows, closes, _ = build_price_history(spot_data)

                atr_val = compute_atr(highs, lows, closes, period=14) if len(closes) >= 15 else None

                technicals = {
                    "rsi":       compute_rsi(closes)             if len(closes) > 0 else 50,
                    "macd":      compute_macd(closes)            if len(closes) > 0 else {},
                    "ema9":      compute_ema(closes, 9)          if len(closes) > 0 else 0,
                    "ema21":     compute_ema(closes, 21)         if len(closes) > 0 else 0,
                    "supertrend":compute_supertrend(highs, lows, closes) if len(closes) > 0 else "NEUTRAL",
                    "atr":       atr_val,
                    "history_source": history_source,
                    "bars":      int(len(closes)),
                    "_closes":   closes.tolist() if hasattr(closes, "tolist") else list(closes),
                    "_highs":    highs.tolist()  if hasattr(highs,  "tolist") else list(highs),
                    "_lows":     lows.tolist()   if hasattr(lows,   "tolist") else list(lows),
                }

                signal = _ai.generate_signal(
                    symbol, spot_data, oc_analysis, technicals, vix_data,
                    now_ist=now_ist, cpr_data=cpr_cache.get(symbol),
                    streak_data=streak_cache.get(symbol),
                    perf_data=perf_cache.get(symbol)
                )

                if verbose:
                    pcr = oc_analysis["pcr_total"] if oc_analysis else "N/A"
                    print(f"[AI Signals] {symbol} spot={spot_data['price']} "
                          f"signal={signal['signal']}@{signal['confidence']:.0f}% "
                          f"tier={signal['push_tier']} PCR={pcr}")

                results[symbol] = {
                    "spot": spot_data, "oi": None,
                    "option_chain": oc_analysis, "breadth": breadth,
                    "indicators": {k: v for k, v in technicals.items() if not k.startswith("_")},
                    "signal": signal,
                }
            elif verbose:
                print("[AI Signals] SENSEX spot unavailable — skipping")
        except Exception as e:
            print(f"[AI Signals] SENSEX failed (skipping): {e}", flush=True)

    return _sanitize({
        "timestamp": now_ist.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "vix": vix_data,
        "data": results,
    })


@app.route("/api/signals")
def api_signals():
    try:
        now_ist = datetime.now(tz=IST)

        # ── Hard gate: market hours ──
        if not is_market_open(now_ist):
            return jsonify({
                "success": True,
                "market_open": False,
                "message": "Market Closed — Live signals resume at 09:15 IST next trading session.",
                "timestamp": now_ist.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "data": None,
            })

        # ── Optional holiday check (NSE marketStatus is authoritative) ──
        ms = nse.fetch_market_status()
        if ms and isinstance(ms.get("marketState"), list):
            cm = next((s for s in ms["marketState"] if s.get("market") == "Capital Market"), None)
            if cm and cm.get("marketStatus") == "Closed" and not (
                now_ist.hour < 9 or (now_ist.hour == 9 and now_ist.minute < 15)
            ):
                return jsonify({
                    "success": True,
                    "market_open": False,
                    "message": "Market Closed (trading holiday or early close).",
                    "timestamp": now_ist.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "data": None,
                })

        payload = build_signals_payload(now_ist)
        payload["success"] = True
        payload["market_open"] = True
        return jsonify(payload)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/debug")
def api_debug():
    """Debug endpoint to test NSE connectivity."""
    results = {}

    # Test 1: Cookies
    try:
        nse._cookies_at = None
        cookie_ok = nse._ensure_cookies()
        cookies = list(nse._session.cookies.keys())
        results["cookies"] = {"success": cookie_ok, "cookies": cookies}
    except Exception as e:
        results["cookies"] = {"success": False, "error": str(e)}

    # Test 2: allIndices (VIX + spot prices)
    try:
        nse._cache.pop("allIndices", None)
        data = nse.fetch_all_indices()
        if data and "data" in data:
            vix = nse.fetch_vix()
            spot = nse.fetch_spot_price("NIFTY")
            results["allIndices"] = {
                "success": True,
                "indexCount": len(data["data"]),
                "vix": vix,
                "niftySpot": spot["price"],
            }
        else:
            results["allIndices"] = {"success": False, "data": str(data)[:200]}
    except Exception as e:
        results["allIndices"] = {"success": False, "error": str(e)}

    # Test 3: OI Spurts
    try:
        nse._cache.pop("oiSpurts", None)
        oi_data = nse.fetch_oi_data()
        if oi_data and "data" in oi_data:
            analysis = analyze_oi_spurts(oi_data, "NIFTY")
            results["oiSpurts"] = {
                "success": True,
                "niftyAnalysis": analysis,
            }
        else:
            results["oiSpurts"] = {"success": False, "data": str(oi_data)[:200]}
    except Exception as e:
        results["oiSpurts"] = {"success": False, "error": str(e)}

    # Test 4: Market Breadth
    try:
        nse._cache.pop("equityNifty50", None)
        breadth = nse.fetch_market_breadth()
        results["marketBreadth"] = {
            "success": breadth["advances"] + breadth["declines"] > 0,
            "data": breadth,
        }
    except Exception as e:
        results["marketBreadth"] = {"success": False, "error": str(e)}

    return jsonify(results)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080, use_reloader=False)
