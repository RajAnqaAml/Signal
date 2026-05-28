"""ai_filter.py — Gemini-powered signal validator with Google Search grounding.

Two-step approach:
  Step 1 (gemini-2.0-flash + search): Fetch today's Indian market events/news
  Step 2 (gemini-2.0-flash, no search): Evaluate signal given the context

Why two steps?
  Gemini 2.5 Flash burns its output budget on "thinking" tokens, leaving no room
  for the actual response (max_output_tokens applies to thinking+text combined).
  Gemini 2.0 Flash has no thinking overhead and gives clean structured output.
  Separating the search step also prevents TOO_MANY_TOOL_CALLS on the long prompt.

Usage (from recorder.py):
    from ai_filter import evaluate_signal
    from market_context import build_context

    ctx = build_context(symbol, signal_block, payload)
    result = evaluate_signal(symbol, signal_block, ctx)
    # result = {"verdict": "CONFIRM|CAUTION|SKIP", "reason": "...",
    #           "risk": "LOW|MEDIUM|HIGH", "key_concern": "..."}

Env var required: GOOGLE_API_KEY
"""
import os
import re
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

_MODEL = "gemini-2.0-flash"   # no thinking tokens, clean structured output

# ─── Prompt templates ─────────────────────────────────────────────────────────

_NEWS_PROMPT = (
    "In 2-3 sentences: are there any major Indian stock market events on {date} "
    "(market holidays, RBI/SEBI announcements, union budget, key macro events) "
    "that could affect {symbol} options trading?"
)

_EVAL_PROMPT = """You are a seasoned Indian intraday options trader specializing in index options.

SIGNAL DETAILS:
- Instrument: {symbol} | Exchange: {exchange} | Direction: {direction} | Time: {time}
- Signal Score: {score}/100 | ADX: {adx} | RSI: {rsi}
- India VIX: {vix}

INSTRUMENT CONTEXT:
- Lot Size: {lot_size} | Strike Gap: {strike_gap} pts
- Expiry: {expiry_day} | Days to Expiry: {dte}
- Estimated ATM Premium: Rs{atm_premium} | IV: {iv}%

MARKET CONTEXT:
- SGX Nifty / Gift Nifty pre-market: {sgx_change}
- US market (previous close): {us_market}
- Today's events & news: {news_context}

TRADE PARAMETERS:
- Action: Buy 1 lot ATM {direction} option on {symbol}
- Capital at Risk: Rs{capital_at_risk}
- Target: Rs1,000-Rs1,500 profit | Stop-loss: Rs500 | Max hold: 2 hours

INSTRUMENT-SPECIFIC RULES (apply silently):
- If BANKNIFTY: be stricter on VIX and RBI/PSU bank news
- If SENSEX: check BSE liquidity; avoid if bid-ask spread on ATM > Rs5
- If NIFTY: most reliable; avoid within 30 min of open or major events
- If DTE <= 1 (expiry day): extreme theta decay -- require score > 75 to CONFIRM
- If DTE >= 4: safer premium -- score > 60 sufficient

VALIDATION (reason silently):
0. Is the market open today? If today is a holiday or market is closed -> SKIP immediately.
1. ADX confirms trend? RSI not stretched against trade?
2. VIX safe? (<14 ideal, 14-18 caution, >20 skip)
3. Global cues (SGX/US) aligned with direction?
4. Any news in next 2 hrs specific to this instrument?
5. Time valid? (avoid 9:15-9:30 open, post 2:30 PM decay)
6. Premium reasonable? (ATM < Rs150 for Rs1,000 target to make sense)

Reply in EXACTLY this format -- no extra text:

VERDICT: CONFIRM or CAUTION or SKIP
REASON: <one crisp sentence covering the strongest factor>
RISK: LOW or MEDIUM or HIGH
KEY_CONCERN: <one specific thing to watch, or None>""".strip()


# ─── Gemini client (lazy init) ────────────────────────────────────────────────
_client = None

def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    return _client


# ─── Response parser ──────────────────────────────────────────────────────────
def _parse(text: str) -> dict:
    def get(key):
        m = re.search(rf"^{key}:\s*(.+)", text, re.MULTILINE | re.IGNORECASE)
        return m.group(1).strip() if m else "UNKNOWN"

    verdict     = get("VERDICT").upper().split()[0]
    risk        = get("RISK").upper().split()[0]
    reason      = get("REASON")
    key_concern = get("KEY_CONCERN")

    if verdict not in ("CONFIRM", "CAUTION", "SKIP"):
        verdict = "CAUTION"
    if risk not in ("LOW", "MEDIUM", "HIGH"):
        risk = "MEDIUM"

    return {"verdict": verdict, "reason": reason, "risk": risk,
            "key_concern": key_concern, "raw": text}


# ─── Main entry point ─────────────────────────────────────────────────────────
def evaluate_signal(symbol: str, signal_block: dict, ctx: dict,
                    now_ist: datetime = None) -> dict:
    """Validate a TIER_1 signal via Gemini before pushing notification.

    Parameters
    ----------
    now_ist : datetime, optional
        Override current time (used for backtesting historical signals).
        Defaults to datetime.now(tz=IST).

    Returns dict: verdict, reason, risk, key_concern, raw.
    On any failure returns CAUTION default — never raises.
    """
    _default = {
        "verdict": "CAUTION",
        "reason": "AI filter unavailable -- proceed with manual check",
        "risk": "MEDIUM",
        "key_concern": "Check news manually",
        "raw": "",
    }
    try:
        if not os.environ.get("GOOGLE_API_KEY"):
            print("[ai_filter] GOOGLE_API_KEY not set -- skipping", flush=True)
            return _default

        from google import genai
        from google.genai import types
        import warnings
        warnings.filterwarnings("ignore")

        client  = _get_client()
        now     = now_ist or datetime.now(tz=IST)
        today   = now.strftime("%d %B %Y")
        time_str = now.strftime("%I:%M %p IST")

        # ── Step 1: fetch market events/news for today ─────────────────────
        news_prompt = _NEWS_PROMPT.format(date=today, symbol=symbol)
        r1 = client.models.generate_content(
            model  = _MODEL,
            contents = news_prompt,
            config = types.GenerateContentConfig(
                tools            = [types.Tool(google_search=types.GoogleSearch())],
                temperature      = 0.0,
                max_output_tokens= 200,
            ),
        )
        news_ctx = (r1.text or "No major events found.").strip()

        # ── Step 2: evaluate signal with all context, no search ────────────
        eval_prompt = _EVAL_PROMPT.format(
            symbol          = symbol,
            exchange        = ctx["exchange"],
            direction       = signal_block.get("signal", "CALL"),
            time            = time_str,
            score           = ctx["score_100"],
            adx             = ctx["adx"],
            rsi             = ctx["rsi"],
            vix             = ctx["vix"],
            lot_size        = ctx["lot_size"],
            strike_gap      = ctx["strike_gap"],
            expiry_day      = ctx["expiry_day"],
            dte             = ctx["dte"],
            atm_premium     = ctx["atm_premium"],
            iv              = ctx["iv"],
            sgx_change      = ctx["sgx_change"],
            us_market       = ctx["us_market"],
            news_context    = news_ctx,
            capital_at_risk = ctx["capital_at_risk"],
        )
        r2 = client.models.generate_content(
            model    = _MODEL,
            contents = eval_prompt,
            config   = types.GenerateContentConfig(
                temperature       = 0.0,
                max_output_tokens = 150,
            ),
        )
        text   = (r2.text or "").strip()
        result = _parse(text)
        result["news_context"] = news_ctx   # store for notification body

        print(
            f"[ai_filter] {symbol} {signal_block.get('signal')} -> "
            f"VERDICT={result['verdict']} RISK={result['risk']} | {result['reason']}",
            flush=True,
        )
        return result

    except Exception as e:
        print(f"[ai_filter] ERROR: {e} -- returning CAUTION default", flush=True)
        return _default


# ─── Standalone smoke test ────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    import json

    mock_signal = {
        "signal": "CALL", "score": 4, "confidence": 62,
        "entry": 24350, "target1": 24420, "stop_loss": 24305,
        "push_tier": "TIER_1", "tier_blocks": [],
        "reasons": ["Supertrend bullish", "RSI 58", "OI buildup CALL side"],
    }
    mock_ctx = {
        "exchange": "NSE", "lot_size": 75, "strike_gap": 50,
        "expiry_day": "Thursday", "dte": 2,
        "atm_premium": 110, "iv": "~17",
        "vix": 14.5, "rsi": 58.0,
        "adx": ">=23 (trending -- gate passed)",
        "score_100": 70,
        "us_market": "S&P 500 +0.5% (prev close 5280)",
        "sgx_change": "N/A -- market open, pre-market cue already priced in",
        "capital_at_risk": 8250,
    }

    print("Testing AI filter (NIFTY CALL mock signal)...")
    result = evaluate_signal("NIFTY", mock_signal, mock_ctx)
    print("\n=== Result ===")
    for k, v in result.items():
        if k != "raw":
            print(f"{k:>15}: {v}")
    print("\nRaw response:")
    print(result["raw"])
