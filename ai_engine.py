"""ai_engine.py — Full AI Signal Engine (replaces rule-based generate_signal).

Architecture:
  Step 1  Gemini + Google Search  →  today's market events / news context
  Step 2  Gemini, no search       →  structured JSON signal from all market data

Input:  same data already collected by build_signals_payload() (spot, option chain,
        technicals, VIX) + market context from market_context.py
Output: signal dict fully compatible with recorder.py / DB / dashboard

Model:  gemini-2.0-flash  (no thinking-token overhead, fast, cheap)
"""

import json
import os
import re
import time
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

import notify

_MODEL   = "gemini-2.0-flash"
_client  = None

# In-session news cache:  (symbol, date_str)  →  news_text
_news_cache: dict = {}

# Expiry config
_EXPIRY_DOW  = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 3}
_EXPIRY_NAME = {"NIFTY": "Tuesday", "BANKNIFTY": "Tuesday", "SENSEX": "Thursday"}


# ─── Gemini client ─────────────────────────────────────────────────────────────

def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    return _client


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _dte(symbol: str, now: datetime) -> int:
    target    = _EXPIRY_DOW.get(symbol, 1)
    days_ahead = (target - now.weekday()) % 7
    return days_ahead


def _format_recent_price(closes, highs, lows, spot, ema9, ema21, atr, supertrend) -> str:
    """Compact price-action summary for last 10 candles."""
    n = min(10, len(closes))
    if n < 2:
        return f"Spot: {spot:.0f} (insufficient history)"

    rows = []
    for i in range(n - 1, -1, -1):            # newest first
        idx  = -(i + 1)
        chg  = closes[idx] - closes[idx - 1] if abs(idx) < len(closes) else 0
        rows.append(f"  {closes[idx]:8.0f}  H:{highs[idx]:8.0f}  L:{lows[idx]:8.0f}  {chg:+6.0f}")

    # Describe recent momentum
    last5_chg = [closes[-j] - closes[-j - 1] for j in range(1, min(6, n))]
    green     = sum(1 for c in last5_chg if c > 0)
    red       = sum(1 for c in last5_chg if c < 0)
    momentum  = "strong bullish" if green >= 4 else "strong bearish" if red >= 4 else "mixed/choppy"

    day_range = max(highs[-n:]) - min(lows[-n:])

    lines = [
        f"Close (newest->oldest):  H:High  L:Low  Chg",
    ] + rows + [
        f"",
        f"Last 5 bars: {green} green / {red} red -> {momentum}",
        f"Session range (last {n} bars): {day_range:.0f} pts  |  ATR(14): {atr:.1f} pts" if atr else f"Session range: {day_range:.0f} pts",
        f"EMA9: {ema9:.0f}  EMA21: {ema21:.0f}  -> {'price ABOVE both (bullish)' if spot > ema9 > ema21 else 'price BELOW both (bearish)' if spot < ema9 < ema21 else 'mixed EMA structure'}",
        f"Supertrend: {supertrend}",
    ]
    return "\n".join(lines)


def _format_option_chain(oc: dict | None, symbol: str) -> str:
    """Format option chain analysis for AI context."""
    if not oc:
        return (
            "Option chain: NSE API currently unavailable. "
            "DO NOT penalize confidence for missing OC — evaluate on technicals, "
            "price action, news, and VIX alone. Full TIER_1 confidence is possible without OC."
        )

    lines = []
    pcr = oc.get("pcr_oi") or oc.get("pcr_total")
    if pcr:
        sentiment = "bullish" if float(pcr) > 1.1 else "bearish" if float(pcr) < 0.9 else "neutral"
        lines.append(f"PCR (OI): {pcr:.2f}  -> {sentiment} institutional positioning")

    mp = oc.get("max_pain")
    if mp:
        lines.append(f"Max Pain: {mp}  (price pulled here at expiry by options writers)")

    # Call wall (resistance)
    cw = oc.get("call_wall") or oc.get("max_call_oi_strike")
    if cw:
        lines.append(f"CALL wall (resistance): {cw} CE  (highest OI concentration)")

    # Put wall (support)
    pw = oc.get("put_wall") or oc.get("max_put_oi_strike")
    if pw:
        lines.append(f"PUT wall (support):     {pw} PE  (highest OI concentration)")

    # Net OI change
    net_oi = oc.get("net_oi_change") or oc.get("oi_change_direction")
    if net_oi:
        lines.append(f"Net OI change today: {net_oi}")

    # IV
    iv = oc.get("atm_iv") or oc.get("iv")
    if iv:
        lines.append(f"ATM IV: {iv:.1f}%  ({'expensive, need big move' if float(iv) > 18 else 'normal range' if float(iv) > 12 else 'cheap, favorable for buying'})")

    return "\n".join(lines) if lines else "Option chain: parsed but key fields missing"


def _get_news_context(symbol: str, now: datetime) -> str:
    """Fetch today's news context via Gemini Search (cached per symbol per day)."""
    date_str = now.strftime("%d %B %Y")
    cache_key = f"{symbol}|{date_str}"

    if cache_key in _news_cache:
        return _news_cache[cache_key]

    try:
        from google import genai
        from google.genai import types
        client = _get_client()

        prompt = (
            f"In 2-3 sentences: any major Indian stock market events on {date_str} "
            f"(market holidays, RBI policy, union budget, major corporate results, SEBI orders, "
            f"global macro events) that affect {symbol} index options trading today?"
        )
        r = client.models.generate_content(
            model    = _MODEL,
            contents = prompt,
            config   = types.GenerateContentConfig(
                tools            = [types.Tool(google_search=types.GoogleSearch())],
                temperature      = 0.0,
                max_output_tokens= 200,
            ),
        )
        ctx = (r.text or "No major events found.").strip()
        _news_cache[cache_key] = ctx
        return ctx
    except Exception as e:
        print(f"[ai_engine] news fetch failed: {e}", flush=True)
        return "News context unavailable."


# ─── Master prompt ──────────────────────────────────────────────────────────────

_MASTER_PROMPT = """
You are an expert NSE/BSE index options prop trader with deep experience in
intraday scalping. Analyze ALL the data below and generate a precise trading signal.

=== INSTRUMENT ===
Symbol: {symbol} | Exchange: {exchange} | DTE: {dte} days to {expiry_day} expiry
Time: {time} | Date: {date}
Lot size: {lot_size} | Strike step: {strike_step} pts
Approx ATM premium: Rs{atm_premium} | Capital at risk: Rs{capital_at_risk}

=== SPOT PRICE ===
Current: {spot} | Day change: {change_pct}% | Open: {open_price}
Day High: {day_high} | Day Low: {day_low}

=== PRICE ACTION & TECHNICALS ===
{price_action}

=== OPTION CHAIN (Smart Money Positioning) ===
{option_chain}

=== MARKET CONTEXT ===
India VIX: {vix} ({vix_trend}) — {vix_desc}
US market (prev close): {us_market}
Pre-market SGX/Gift Nifty: {sgx}
Today's events & news: {news_context}

=== TRADE CONSTRAINTS ===
Target: Rs 1,000-1,500 profit (1 lot) | Stop-loss: Rs 500 | Max hold: 2 hours
Avoid: market open first 15 min (before 9:30), last 30 min (after 15:00), expiry day (DTE=0)

=== YOUR ANALYSIS FRAMEWORK ===
Think through these silently before deciding:
1. THE SNIPER RULE: If the probability of success is not overwhelmingly high (>90%), output WAIT. Do not force trades. You are a sniper, not a machine gunner.
2. TREND: Are EMAs aligned? Is price making higher highs/lows or lower highs/lows? If not perfectly clear, WAIT.
3. MOMENTUM: Last 5 candles — continuation or exhaustion? If exhaustion is visible, WAIT.
4. OPTION CHAIN: Where is max OI? That is S/R. Is price moving toward or away from max pain? If OC shows "unavailable" — SKIP this step, do NOT reduce confidence.
5. SMART MONEY: PCR + net OI change = are institutions adding bullish or bearish bets? If OC unavailable — SKIP, do NOT reduce confidence.
6. VOLATILITY: VIX + IV — is premium buying justified? ATR = expected move per bar
7. RISK: Any event in next 2 hrs? DTE risk? Time of day risk?
8. REGIME: Is this a trending day (strong momentum, aligned indicators) or choppy day?

=== OUTPUT ===
Output ONLY valid JSON. No markdown fences, no explanation outside JSON:
{{
  "signal": "CALL" or "PUT" or "WAIT",
  "confidence": <integer 0-100>,
  "regime": "TRENDING" or "CHOPPY" or "REVERSING",
  "entry_spot": {spot},
  "target_pts": <integer — spot points to T1, calibrated to ATR>,
  "sl_pts": <integer — spot points for stop-loss, ~50-60% of target>,
  "hold_minutes": <estimated hold time in minutes, max 120>,
  "smart_money": "<one sentence: what OI structure reveals about institutional positioning>",
  "reasoning": "<2-3 sentences: the STORY behind this trade — why now, why this direction>",
  "key_risk": "<one specific thing that could kill this trade>",
  "push_tier": "TIER_1" or "TIER_2" or "TIER_3"
}}

push_tier rules (be strict — protect capital):
TIER_1: confidence >= 85 AND regime=TRENDING AND signal != WAIT AND DTE >= 1 AND no blocking event
TIER_2: confidence 65-84 OR regime=CHOPPY (informational — dashboard only, do not notify phone)
TIER_3: confidence < 65 OR signal=WAIT OR DTE=0 OR major event in next 2 hrs
""".strip()


# ─── Main signal generator ─────────────────────────────────────────────────────

def generate_signal(
    symbol: str,
    spot_data: dict,
    oc_analysis: dict | None,
    technicals: dict,
    vix_data: dict,
    now_ist: datetime = None,
) -> dict:
    """AI-powered signal generator. Drop-in replacement for rule-based generate_signal().

    Parameters
    ----------
    symbol      : 'NIFTY' | 'BANKNIFTY' | 'SENSEX'
    spot_data   : {price, change, open, high, low, prev_close}
    oc_analysis : output of analyze_option_chain(), or None
    technicals  : {rsi, ema9, ema21, supertrend, atr, adx_di, ...}
    vix_data    : {value, change}
    now_ist     : current IST datetime (for backtesting pass historical dt)

    Returns
    -------
    dict  compatible with recorder.py / DB / dashboard signal format
    """
    _wait = _make_wait(spot_data, symbol)

    try:
        if not os.environ.get("GOOGLE_API_KEY"):
            print(f"[ai_engine] GOOGLE_API_KEY not set — returning WAIT", flush=True)
            return _wait

        from google import genai
        from google.genai import types
        client = _get_client()

        now      = now_ist or datetime.now(tz=IST)
        date_str = now.strftime("%d %B %Y")
        time_str = now.strftime("%I:%M %p IST")

        spot     = float(spot_data.get("price", 0))
        exchange = "BSE" if symbol == "SENSEX" else "NSE"
        lot      = notify.LOT_SIZE.get(symbol, 75)
        step     = notify.STRIKE_STEP.get(symbol, 50)
        dte      = _dte(symbol, now)

        # ATM premium estimate
        atr = float(technicals.get("atr") or 0)
        import math
        atm_est  = int(atr * 0.55 * math.sqrt(max(dte, 0.5))) if atr > 5 else {
            "NIFTY": 110, "BANKNIFTY": 280, "SENSEX": 320
        }.get(symbol, 110)
        capital  = lot * atm_est

        # VIX
        vix_val   = float(vix_data.get("value", 15) if isinstance(vix_data, dict) else 15)
        vix_chg   = float(vix_data.get("change", 0) if isinstance(vix_data, dict) else 0)
        vix_trend = f"{'+' if vix_chg >= 0 else ''}{vix_chg:.1f} today"
        vix_desc  = ("low fear — good for option buying" if vix_val < 14
                     else "elevated — widen your SL" if vix_val > 18
                     else "moderate range")

        # Price action
        closes = technicals.get("_closes") or []
        highs  = technicals.get("_highs")  or []
        lows   = technicals.get("_lows")   or []
        pa_str = _format_recent_price(
            closes, highs, lows, spot,
            ema9       = float(technicals.get("ema9")  or spot),
            ema21      = float(technicals.get("ema21") or spot),
            atr        = atr,
            supertrend = technicals.get("supertrend", "N/A"),
        )

        # Option chain
        oc_str = _format_option_chain(oc_analysis, symbol)

        # Market context
        from market_context import _get_us_market, _get_gift_nifty
        us_market = _get_us_market()
        sgx       = _get_gift_nifty(now)

        # News context (step 1 — with search)
        news_ctx  = _get_news_context(symbol, now)

        # Build full prompt
        prompt = _MASTER_PROMPT.format(
            symbol         = symbol,
            exchange       = exchange,
            dte            = dte,
            expiry_day     = _EXPIRY_NAME.get(symbol, "Tuesday"),
            time           = time_str,
            date           = date_str,
            lot_size       = lot,
            strike_step    = step,
            atm_premium    = atm_est,
            capital_at_risk= capital,
            spot           = spot,
            change_pct     = spot_data.get("change", 0),
            open_price     = spot_data.get("open", spot),
            day_high       = spot_data.get("high", spot),
            day_low        = spot_data.get("low", spot),
            price_action   = pa_str,
            option_chain   = oc_str,
            vix            = vix_val,
            vix_trend      = vix_trend,
            vix_desc       = vix_desc,
            us_market      = us_market,
            sgx            = sgx,
            news_context   = news_ctx,
        )

        # Step 2 — AI analysis (no search, consistent JSON output)
        r = client.models.generate_content(
            model    = _MODEL,
            contents = prompt,
            config   = types.GenerateContentConfig(
                temperature       = 0.1,
                max_output_tokens = 400,
            ),
        )
        raw = (r.text or "").strip()

        ai_signal = _parse_ai_response(raw, spot, symbol, atr, dte)
        result    = _to_signal_dict(ai_signal, spot_data, symbol, now, technicals)

        print(
            f"[ai_engine] {symbol} -> {result['signal']} "
            f"conf={result['confidence']:.0f}% tier={result['push_tier']} "
            f"regime={ai_signal.get('regime','?')} | {ai_signal.get('reasoning','')[:60]}",
            flush=True,
        )
        return result

    except Exception as e:
        import traceback as tb
        print(f"[ai_engine] ERROR for {symbol}: {e}", flush=True)
        tb.print_exc()
        return _wait


# ─── Response parsing & validation ────────────────────────────────────────────

def _parse_ai_response(raw: str, spot: float, symbol: str, atr: float, dte: int) -> dict:
    """Extract and validate JSON from Gemini response."""
    # Strip any markdown fences
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()

    # Try to extract JSON object
    m = re.search(r"\{[\s\S]+\}", cleaned)
    if not m:
        return {"signal": "WAIT", "confidence": 0, "regime": "CHOPPY",
                "reasoning": "Could not parse AI response", "push_tier": "TIER_3"}

    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        # Try to fix common issues
        fixed = re.sub(r",\s*}", "}", cleaned)
        try:
            obj = json.loads(re.search(r"\{[\s\S]+\}", fixed).group())
        except Exception:
            return {"signal": "WAIT", "confidence": 0, "regime": "CHOPPY",
                    "reasoning": "JSON parse error in AI response", "push_tier": "TIER_3"}

    # Normalize and validate fields
    sig = str(obj.get("signal", "WAIT")).upper()
    if sig not in ("CALL", "PUT", "WAIT"):
        sig = "WAIT"

    conf = int(max(0, min(100, obj.get("confidence", 0) or 0)))
    regime = str(obj.get("regime", "CHOPPY")).upper()
    if regime not in ("TRENDING", "CHOPPY", "REVERSING"):
        regime = "CHOPPY"

    # Target & SL validation — clamp to sensible ATR multiples
    atr_ref = atr if atr and atr > 5 else {
        "NIFTY": 30, "BANKNIFTY": 120, "SENSEX": 150
    }.get(symbol, 30)

    t1_pts = int(max(atr_ref * 0.5, min(atr_ref * 4, obj.get("target_pts", atr_ref) or atr_ref)))
    sl_pts = int(max(atr_ref * 0.3, min(t1_pts * 0.8, obj.get("sl_pts", int(t1_pts * 0.55)) or int(t1_pts * 0.55))))

    # push_tier override: safety rules that override AI's suggestion
    tier = str(obj.get("push_tier", "TIER_3")).upper()
    if dte == 0:
        tier = "TIER_3"               # expiry day — never TIER_1
    if sig == "WAIT" or conf < 50:
        tier = "TIER_3"
    if tier not in ("TIER_1", "TIER_2", "TIER_3"):
        tier = "TIER_3"

    return {
        "signal":       sig,
        "confidence":   conf,
        "regime":       regime,
        "entry_spot":   float(obj.get("entry_spot", spot)),
        "target_pts":   t1_pts,
        "sl_pts":       sl_pts,
        "hold_minutes": int(min(120, max(10, obj.get("hold_minutes", 60) or 60))),
        "smart_money":  str(obj.get("smart_money", "") or ""),
        "reasoning":    str(obj.get("reasoning", "") or ""),
        "key_risk":     str(obj.get("key_risk", "") or ""),
        "push_tier":    tier,
        "raw_json":     raw,
    }


def _to_signal_dict(ai: dict, spot_data: dict, symbol: str, now: datetime,
                    technicals: dict) -> dict:
    """Convert AI parsed dict → legacy signal dict (recorder/DB compatible)."""
    spot      = float(spot_data.get("price", 0))
    direction = ai["signal"]
    tier      = ai["push_tier"]
    conf      = ai["confidence"]
    t1_pts    = ai["target_pts"]
    sl_pts    = ai["sl_pts"]

    if direction == "CALL":
        target1   = round(spot + t1_pts)
        stop_loss = round(spot - sl_pts)
        score     = round((conf - 50) / 5, 1)          # 50→0, 75→+5, 100→+10
    elif direction == "PUT":
        target1   = round(spot - t1_pts)
        stop_loss = round(spot + sl_pts)
        score     = -round((conf - 50) / 5, 1)
    else:  # WAIT
        target1   = 0
        stop_loss = 0
        score     = 0

    # Build reasons list from AI text fields
    reasons = []
    if ai.get("reasoning"):
        reasons.append(ai["reasoning"])
    if ai.get("smart_money"):
        reasons.append(f"OI: {ai['smart_money']}")
    if ai.get("key_risk"):
        reasons.append(f"Risk: {ai['key_risk']}")
    if ai.get("regime"):
        reasons.append(f"Regime: {ai['regime']}")

    return {
        "signal":         direction,
        "confidence":     float(conf),
        "score":          score,
        "trend_score":    0,
        "oi_score":       0,
        "gap_weight":     0.0,
        "reasons":        reasons,
        "entry":          spot,
        "target1":        target1,
        "target2":        round(target1 + (target1 - spot)) if direction != "WAIT" else 0,
        "stop_loss":      stop_loss,
        "evidence_quality": "ai",
        "push_tier":      tier,
        "tier_blocks":    [],
        # Extra AI fields (shown in notifications)
        "ai_regime":      ai.get("regime", ""),
        "ai_reasoning":   ai.get("reasoning", ""),
        "ai_key_risk":    ai.get("key_risk", ""),
        "ai_hold_min":    ai.get("hold_minutes", 60),
    }


def _make_wait(spot_data: dict, symbol: str) -> dict:
    """Return a neutral WAIT signal dict."""
    spot = float(spot_data.get("price", 0) if spot_data else 0)
    return {
        "signal": "NEUTRAL", "confidence": 0.0, "score": 0,
        "trend_score": 0, "oi_score": 0, "gap_weight": 0.0,
        "reasons": [], "entry": spot,
        "target1": 0, "target2": 0, "stop_loss": 0,
        "evidence_quality": "ai", "push_tier": "TIER_3",
        "tier_blocks": [], "ai_regime": "CHOPPY",
        "ai_reasoning": "", "ai_key_risk": "", "ai_hold_min": 0,
    }


# ─── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    import numpy as np

    spot = 24350.0
    mock_spot   = {"price": spot, "change": 0.4, "open": 24280.0, "high": 24390.0, "low": 24255.0}
    mock_oc     = {
        "pcr_oi": 1.12, "max_pain": 24200, "call_wall": 24500, "put_wall": 24000,
        "atm_iv": 13.8, "net_oi_change": "CALL OI +12%, PUT OI -6% (bullish)"
    }
    closes = np.linspace(24255, 24350, 50)
    highs  = closes + 20
    lows   = closes - 20
    from app import compute_ema, compute_atr, compute_supertrend
    mock_tech   = {
        "rsi": 58.0,
        "ema9":  float(compute_ema(closes, 9)),
        "ema21": float(compute_ema(closes, 21)),
        "supertrend": str(compute_supertrend(highs, lows, closes)),
        "atr":   float(compute_atr(highs, lows, closes)),
        "history_source": "real",
        "bars":  50,
        "_closes": closes.tolist(),
        "_highs":  highs.tolist(),
        "_lows":   lows.tolist(),
    }
    mock_vix = {"value": 14.5, "change": -0.3}

    print("Testing AI Engine with mock NIFTY data...")
    sig = generate_signal("NIFTY", mock_spot, mock_oc, mock_tech, mock_vix)
    print("\n=== Signal ===")
    for k, v in sig.items():
        if k != "raw_json":
            print(f"  {k:20s}: {v}")
