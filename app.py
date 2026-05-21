import json
import math
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from curl_cffi import requests as cfreq
from flask import Flask, jsonify, render_template
from flask_cors import CORS

IST = ZoneInfo("Asia/Kolkata")


def is_market_open(now=None):
    """NSE cash/F&O session: Mon-Fri 09:15-15:30 IST.
    Holiday calendar is not handled here — /api/marketStatus is the authoritative
    source for that and is consulted at request time when the clock window matches.
    """
    now = now or datetime.now(tz=IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
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
        """Fetch equity stock indices for market breadth (advances/declines)."""
        data = self._fetch_json(
            "/api/equity-stockIndices?index=NIFTY%2050",
            referer=f"{self.BASE_URL}/market-data/live-equity-market",
            cache_key="equityNifty50",
        )
        if data and "advance" in data:
            adv = data["advance"]
            return {
                "advances": int(adv.get("advances", 0)),
                "declines": int(adv.get("declines", 0)),
                "unchanged": int(adv.get("unchanged", 0)),
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
        """Fetch full option chain for NIFTY or BANKNIFTY."""
        return self._fetch_json(
            f"/api/option-chain-indices?symbol={symbol}",
            referer=f"{self.BASE_URL}/option-chain",
            cache_key=f"optionChain_{symbol}",
            cache_ttl=20,
        )

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
def analyze_option_chain(oc_data, spot, symbol="NIFTY"):
    """Compute PCR, Max Pain, support/resistance strikes, ATM OI flow, IV skew
    from the NSE option-chain payload. Operates on the **nearest expiry only**
    so signals reflect this-week positioning, not far-month noise.
    """
    if not oc_data or "records" not in oc_data:
        return None

    records = oc_data["records"]
    rows = records.get("data", [])
    expiries = records.get("expiryDates", [])
    if not rows or not expiries:
        return None

    nearest_expiry = expiries[0]
    step = 50 if symbol == "NIFTY" else 100

    strikes = {}  # strike -> {"ce_oi", "pe_oi", "ce_chg", "pe_chg", "ce_iv", "pe_iv"}
    for row in rows:
        if row.get("expiryDate") != nearest_expiry:
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

    # IV skew over ATM ±5, ignoring zero-IV (illiquid) strikes.
    iv_band = [atm_strike + i * step for i in range(-5, 6)]
    ce_ivs = [strikes[k]["ce_iv"] for k in iv_band if k in strikes and strikes[k]["ce_iv"] > 0]
    pe_ivs = [strikes[k]["pe_iv"] for k in iv_band if k in strikes and strikes[k]["pe_iv"] > 0]
    iv_skew = round(np.mean(pe_ivs) - np.mean(ce_ivs), 2) if ce_ivs and pe_ivs else 0

    return {
        "expiry": nearest_expiry,
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


# ─── Signal Generator ───────────────────────────────────────────────────────
def _gap_decay_weight(now_ist):
    """Gap factor decay: 1.0 at market open (09:15 IST), linear -> 0.0 at 12:15 IST.
    Rationale: a gap is high-information at open but is fully priced in within ~3 hours.
    Without this, gap-down keeps penalizing the score all session even after recovery.
    """
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    minutes_since_open = (now_ist - market_open).total_seconds() / 60
    if minutes_since_open <= 0:
        return 1.0
    return max(0.0, 1.0 - minutes_since_open / 180)


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
    intraday_pct = ((spot - day_open) / day_open * 100) if day_open > 0 else 0
    if intraday_pct > 0.8:
        trend_score += 2
        reasons.append(f"Strong intraday up-move +{intraday_pct:.2f}% from open -> Bullish momentum")
    elif intraday_pct > 0.3:
        trend_score += 1
        reasons.append(f"Positive intraday move +{intraday_pct:.2f}% from open")
    elif intraday_pct < -0.8:
        trend_score -= 2
        reasons.append(f"Strong intraday down-move {intraday_pct:.2f}% from open -> Bearish momentum")
    elif intraday_pct < -0.3:
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

    # ── Factor 3: Gap Analysis (Open vs Prev Close) with time-decay ──
    # Gap matters at 09:15 but is fully priced in by 12:15. Decay weight handles this.
    gap_pct = ((day_open - prev_close) / prev_close * 100) if prev_close > 0 else 0
    if gap_pct > 0.3:
        contribution = 1.0 * gap_weight
        trend_score += contribution
        reasons.append(f"Gap-up +{gap_pct:.2f}% × decay {gap_weight:.2f} = {contribution:+.2f}")
    elif gap_pct < -0.3:
        contribution = -1.0 * gap_weight
        trend_score += contribution
        reasons.append(f"Gap-down {gap_pct:.2f}% × decay {gap_weight:.2f} = {contribution:+.2f}")

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

    if score >= 3:
        signal = "CALL"
        entry = spot
        target1 = round((spot + step * 1.5) / step) * step
        target2 = round((spot + step * 3) / step) * step
        stop_loss = round((spot - step * 1.2) / step) * step
    elif score <= -3:
        signal = "PUT"
        entry = spot
        target1 = round((spot - step * 1.5) / step) * step
        target2 = round((spot - step * 3) / step) * step
        stop_loss = round((spot + step * 1.2) / step) * step
    else:
        signal = "NEUTRAL"
        confidence = 0
        entry = spot
        target1 = 0
        target2 = 0
        stop_loss = 0
        reasons.append(f"Score {score:+.2f} below threshold (|3| required) — no trade")

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
    }


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
    """Build the full data+signal dict for both NIFTY and BANKNIFTY.
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
        oc_analysis = analyze_option_chain(oc_data, spot_data["price"], symbol) if oc_data else None

        chart = nse.fetch_intraday_chart(symbol)
        highs, lows, closes = build_real_history(chart)
        if len(closes) >= 30:
            history_source = "real"
            prices = closes
        else:
            history_source = "synthetic"
            highs, lows, closes, prices = build_price_history(spot_data)

        technicals = {
            "rsi": compute_rsi(prices) if len(prices) > 0 else 50,
            "macd": compute_macd(prices) if len(prices) > 0 else {"macd": 0, "signal": 0, "histogram": 0},
            "ema9": compute_ema(prices, 9) if len(prices) > 0 else 0,
            "ema21": compute_ema(prices, 21) if len(prices) > 0 else 0,
            "supertrend": compute_supertrend(highs, lows, closes) if len(closes) > 0 else "NEUTRAL",
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

    return {
        "timestamp": now_ist.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "vix": vix_data,
        "data": results,
    }


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
