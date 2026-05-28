"""market_context.py — Fetch external market context for AI signal filter.

Provides:
  build_context(symbol, signal_block, payload) → dict

All external calls are best-effort — failures return "Unavailable" strings,
never raise. This ensures AI filter never blocks the notification path.
"""
import math
import os
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

# Expiry config
_EXPIRY_DOW  = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 3}   # Mon=0, Tue=1, Thu=3
_EXPIRY_NAME = {"NIFTY": "Tuesday", "BANKNIFTY": "Tuesday", "SENSEX": "Thursday"}

# Fallback ATM premium estimates when ATR is unavailable (₹)
_ATM_FALLBACK = {"NIFTY": 110, "BANKNIFTY": 280, "SENSEX": 320}


# ─── External data fetchers (all best-effort) ────────────────────────────────

def _get_us_market() -> str:
    """S&P 500 previous-session change (always available after 22:00 IST)."""
    try:
        import yfinance as yf
        hist = yf.Ticker("^GSPC").history(period="3d", auto_adjust=True)
        if len(hist) >= 2:
            prev = hist["Close"].iloc[-2]
            last = hist["Close"].iloc[-1]
            chg  = (last - prev) / prev * 100
            arrow = "+" if chg > 0 else "-"
            return f"S&P 500 {arrow}{abs(chg):.1f}% (prev close {last:,.0f})"
    except Exception as e:
        pass
    return "Unavailable"


def _get_gift_nifty(now: datetime) -> str:
    """Gift Nifty pre-market cue.

    Only meaningful before 09:30 IST; once the market has been open >15 min
    the cue is already priced in, so we skip the fetch and say so.
    """
    if now.hour > 9 or (now.hour == 9 and now.minute >= 30):
        return "N/A — market open, pre-market cue already priced in"
    try:
        import yfinance as yf
        # Nifty 50 spot as proxy for Gift Nifty direction
        hist = yf.Ticker("^NSEI").history(period="2d", auto_adjust=True)
        if len(hist) >= 2:
            prev = hist["Close"].iloc[-2]
            last = hist["Close"].iloc[-1]
            chg  = last - prev
            pct  = chg / prev * 100
            arrow = "+" if chg > 0 else "-"
            return f"Nifty prev-close {arrow}{abs(pct):.1f}% ({chg:+.0f} pts)"
    except Exception:
        pass
    return "Unavailable"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _dte(symbol: str, now: datetime) -> int:
    """Days to next weekly expiry (minimum 0 = today is expiry)."""
    target = _EXPIRY_DOW.get(symbol, 1)
    days   = (target - now.weekday()) % 7
    return days  # 0 = expiry day, 1-6 = days ahead


def _adx_from_tier_blocks(tier_blocks: list) -> str:
    """Infer ADX strength from tier_blocks set by Gate 10."""
    for b in tier_blocks:
        if "G10" in b:
            if "LOW"     in b: return "<18 (no trend — gate blocked)"
            if "CAUTION" in b: return "18–23 (marginal trend — caution)"
            if "DI_MISS" in b: return "≥18 but DI misaligned"
    return "≥23 (trending — gate passed)"


def _atm_premium_estimate(symbol: str, atr: float, dte: int) -> int:
    """Rough ATM option premium estimate.

    Formula: atr * iv_scalar * sqrt(dte_fraction_of_week)
    Clamped to a sane range per instrument.
    """
    if atr and atr > 5:
        raw = atr * 0.55 * math.sqrt(max(dte, 0.5))
        return max(30, int(raw))
    return _ATM_FALLBACK.get(symbol, 110)


# ─── Main builder ─────────────────────────────────────────────────────────────

def build_context(symbol: str, signal_block: dict, payload: dict) -> dict:
    """Build the full context dict needed by ai_filter.evaluate_signal().

    Parameters
    ----------
    symbol       : 'NIFTY' | 'BANKNIFTY' | 'SENSEX'
    signal_block : the signal dict from payload['data'][symbol]['signal']
    payload      : full output of build_signals_payload()
    """
    from notify import LOT_SIZE, STRIKE_STEP

    now = datetime.now(tz=IST)

    # ── Indicators from payload ──────────────────────────────────────────────
    sym_data   = payload.get("data", {}).get(symbol, {})
    indicators = sym_data.get("indicators", {}) or {}
    rsi        = round(float(indicators.get("rsi", 50) or 50), 1)
    atr        = float(indicators.get("atr", 0) or 0)

    # ── VIX ─────────────────────────────────────────────────────────────────
    vix_data = payload.get("vix", {})
    vix      = round(float(vix_data.get("value", 0) if isinstance(vix_data, dict) else 0), 2)

    # ── Contract specs ───────────────────────────────────────────────────────
    lot  = LOT_SIZE.get(symbol, 75)
    step = STRIKE_STEP.get(symbol, 50)

    # ── Expiry / DTE ─────────────────────────────────────────────────────────
    dte        = _dte(symbol, now)
    expiry_day = _EXPIRY_NAME.get(symbol, "Tuesday")

    # ── ADX from tier_blocks (Gate 10) ───────────────────────────────────────
    adx_str = _adx_from_tier_blocks(signal_block.get("tier_blocks", []))

    # ── ATM premium estimate ─────────────────────────────────────────────────
    atm_premium = _atm_premium_estimate(symbol, atr, dte)

    # ── Score: normalize engine score (-10→+10) to 0→100 ────────────────────
    score_raw = float(signal_block.get("score", 0) or 0)
    score_100 = max(0, min(100, int((score_raw + 10) * 5)))

    # ── Capital at risk ──────────────────────────────────────────────────────
    capital_at_risk = lot * atm_premium

    # ── External cues (best-effort) ──────────────────────────────────────────
    us_market  = _get_us_market()
    sgx_change = _get_gift_nifty(now)

    # ── IV proxy (India VIX is the index-wide IV benchmark) ─────────────────
    iv_proxy = f"~{int(vix * 1.05)}" if vix else "N/A"

    return {
        "exchange":        "BSE" if symbol == "SENSEX" else "NSE",
        "lot_size":        lot,
        "strike_gap":      step,
        "expiry_day":      expiry_day,
        "dte":             dte,
        "atm_premium":     atm_premium,
        "iv":              iv_proxy,
        "vix":             vix,
        "rsi":             rsi,
        "adx":             adx_str,
        "score_100":       score_100,
        "us_market":       us_market,
        "sgx_change":      sgx_change,
        "capital_at_risk": capital_at_risk,
    }
