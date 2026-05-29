"""Push notifications via ntfy.sh — free, no signup, mobile push.

Usage:
    import notify
    notify.send_signal_alert("NIFTY", row)              # send alert if actionable
    notify.send_test()                                  # test message

Env vars:
    NTFY_TOPIC   — required; the topic string. Treat like a password
                   (anyone with this string can read AND send your alerts).
    NTFY_SERVER  — optional; default https://ntfy.sh.

Subscribe on phone: install the ntfy app (Android/iOS), tap "+", paste topic name.
"""
import os
import urllib.request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Lot sizes and strike-spacing (NSE post-2024 revision)
LOT_SIZE = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}
TARGET_PTS = {"NIFTY": 30, "BANKNIFTY": 60, "SENSEX": 100}
SL_PTS = {"NIFTY": 18, "BANKNIFTY": 30, "SENSEX": 50}
ATM_DELTA = 0.50  # rough — ATM weekly delta


def recommend_strike(symbol: str, spot: float) -> int:
    """Return the ATM strike — nearest multiple of STRIKE_STEP to spot."""
    step = STRIKE_STEP.get(symbol, 50)
    return int(round(spot / step) * step)


def estimate_pnl(symbol: str, points: float) -> int:
    """Approximate ₹ P&L from spot-point move using ATM delta and lot size."""
    return int(round(abs(points) * ATM_DELTA * LOT_SIZE.get(symbol, 75)))


def _topic_url():
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return None
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    return f"{server}/{topic}"


def is_configured() -> bool:
    return bool(os.environ.get("NTFY_TOPIC"))


def _post(url: str, title: str, body: str, priority: str = "default", tags=None,
          timeout: int = 10) -> bool:
    """POST to ntfy with proper headers. Returns True if HTTP 2xx, else False."""
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", errors="ignore"),
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[notify] POST failed: {e}", flush=True)
        return False


def send_signal_alert(symbol: str, signal_row: dict) -> bool:
    """Send an actionable-trade alert.
    Format is mobile-optimized: title shows the strike to BUY; body shows
    capital, ₹ targets, and reasons.

    signal_row: same dict shape as the DB row (db.get_snapshots) or the
    per-symbol slice of build_signals_payload.
    """
    url = _topic_url()
    if not url:
        return False

    direction = signal_row.get("signal", "NEUTRAL")
    if direction == "NEUTRAL":
        return False

    spot = signal_row.get("spot_price") or signal_row.get("entry") or 0
    spot = float(spot)
    score = float(signal_row.get("score", 0) or 0)
    conf = float(signal_row.get("confidence", 0) or 0)
    oi = float(signal_row.get("oi_score", 0) or 0)
    reasons = signal_row.get("reasons") or []

    # Strike + option type
    strike = recommend_strike(symbol, spot)
    opt_type = "PE" if direction == "PUT" else "CE"
    option = f"{strike} {opt_type}"

    # Targets in spot points (using the scalp rule, not the engine's wider T1/SL)
    target_pts = TARGET_PTS.get(symbol, 30)
    sl_pts = SL_PTS.get(symbol, 15)
    target_spot = spot - target_pts if direction == "PUT" else spot + target_pts
    sl_spot = spot + sl_pts if direction == "PUT" else spot - sl_pts

    # Rupee estimates
    target_inr = estimate_pnl(symbol, target_pts)
    sl_inr = estimate_pnl(symbol, sl_pts)
    lot = LOT_SIZE.get(symbol, 75)

    # Tier
    has_contrarian = any(("Contrarian" in r or "Sharp" in r) for r in reasons)
    if abs(score) >= 4 and conf >= 48 and abs(oi) >= 2 and not has_contrarian:
        tier = "GREEN (high conviction)"
    elif abs(score) >= 3 and conf >= 30 and not has_contrarian:
        tier = "YELLOW (paper-trade)"
    else:
        tier = "RED (skip)"

    # Title — fits on phone lock-screen (~50 chars)
    icon = "[PUT]" if direction == "PUT" else "[CALL]"
    title = f"{icon} {symbol}: BUY {option}"

    # Body — compact, scannable. NOTE: removed "exit in 10 min" line because
    # engine signals routinely sustain conviction for HOURS (Mon BN ran 5+ hrs).
    # Exit guidance now follows the engine state, not a fixed time cap.
    # AI engine fields (new)
    ai_reasoning = signal_row.get("ai_reasoning", "")
    ai_regime    = signal_row.get("ai_regime", "")
    ai_key_risk  = signal_row.get("ai_key_risk", "")
    ai_hold_min  = signal_row.get("ai_hold_min", 0)
    is_ai_signal = bool(signal_row.get("evidence_quality") == "ai" or ai_reasoning)

    body = [
        f"Spot:    {spot:.2f}",
        f"Buy:     {option}  (ATM weekly, {lot}-share lot)",
        "",
        f"Target:  spot {target_spot:.0f}  ({direction == 'PUT' and '-' or '+'}{target_pts} pts)  ≈ +Rs {target_inr}",
        f"Stop:    spot {sl_spot:.0f}  ({direction == 'PUT' and '+' or '-'}{sl_pts} pts)  ≈ -Rs {sl_inr}",
        f"Hold:    max {ai_hold_min} min (exit if signal flips)" if ai_hold_min else
        "Exit:    HOLD while engine stays {dir}. Exit when signal flips.".format(dir=direction),
        "",
        f"Conf: {conf:.0f}%  Regime: {ai_regime}  Tier: {tier}" if ai_regime else
        f"Score: {score:+.2f}  Conf: {conf:.0f}%  Tier: {tier}",
        "",
        "AI Reasoning:",
    ]

    if is_ai_signal and ai_reasoning:
        # Wrap long reasoning into lines
        body.append(f"  {ai_reasoning}")
        if ai_key_risk:
            body.append(f"")
            body.append(f"Risk: {ai_key_risk}")
    else:
        # Legacy rule-based reasons
        body.append("Why:")
        keep = [r for r in reasons if "Real intraday" not in r and "below threshold" not in r][:4]
        for r in keep:
            body.append(f"  - {r}")

    # AI Filter verdict (attached by recorder.py if Gemini filter ran)
    ai_verdict  = signal_row.get("ai_verdict")
    ai_risk     = signal_row.get("ai_risk", "")
    ai_reason   = signal_row.get("ai_reason", "")
    ai_concern  = signal_row.get("ai_concern", "")
    if ai_verdict:
        verdict_icon = {"CONFIRM": "✅", "CAUTION": "⚠️", "SKIP": "🚫"}.get(ai_verdict, "🤖")
        body.append("")
        body.append(f"AI: {verdict_icon} {ai_verdict}  Risk: {ai_risk}")
        if ai_reason:
            body.append(f"    {ai_reason}")
        if ai_concern and ai_concern.lower() != "none":
            body.append(f"Watch: {ai_concern}")

    # Strip arrows so the message is robust across ntfy clients
    body_text = "\n".join(body).replace("→", "->").replace("—", "-")

    priority = "high"
    tags = ["chart_with_downwards_trend"] if direction == "PUT" else ["chart_with_upwards_trend"]
    return _post(url, title, body_text, priority=priority, tags=tags)


def send_watch_alert(symbol: str, signal_row: dict) -> bool:
    """Send a silent WATCH alert for TIER_2 signals.
    Uses ntfy priority=low — arrives in notification tray with no sound/vibration.
    """
    url = _topic_url()
    if not url:
        return False

    direction = signal_row.get("signal", "NEUTRAL")
    if direction in ("NEUTRAL", "WAIT"):
        return False

    spot   = float(signal_row.get("spot_price") or signal_row.get("entry") or 0)
    conf   = float(signal_row.get("confidence", 0) or 0)
    regime = signal_row.get("ai_regime", "")
    reason = (signal_row.get("ai_reasoning", "") or "")[:200]

    icon  = "PUT" if direction == "PUT" else "CALL"
    title = f"[WATCH] {symbol} {icon} {conf:.0f}% — {regime}"
    body  = "\n".join([
        f"Spot: {spot:.2f}  |  Conf: {conf:.0f}%  |  Regime: {regime}",
        "",
        reason if reason else "No AI reasoning.",
        "",
        "TIER_2 watch — not high conviction. No action needed.",
    ])
    tags = ["eyes"]
    return _post(url, title, body, priority="low", tags=tags)


def send_exit_alert(symbol: str, prior_direction: str, current_state: str,
                     entry_spot: float = None, current_spot: float = None,
                     held_min: int = None) -> bool:
    """Push EXIT alert when engine flips OUT of an active direction.
    Different from send_signal_alert (which is for ENTRY). This is the
    'time to close the trade' notification.

    prior_direction: 'CALL' or 'PUT' (what the active trade was)
    current_state:   'NEUTRAL' or the opposite direction (what engine says now)
    """
    url = _topic_url()
    if not url:
        return False

    title = f"[EXIT] {symbol} {prior_direction} — engine flipped to {current_state}"
    body_lines = [
        f"Engine has lost {prior_direction} conviction.",
        f"Active trade direction (if you took it): {prior_direction}",
        f"Engine now signaling: {current_state}",
        "",
    ]
    if entry_spot is not None and current_spot is not None:
        sign = 1 if prior_direction == "CALL" else -1
        delta = (current_spot - entry_spot) * sign
        body_lines.append(f"Entry spot:   {entry_spot:.2f}")
        body_lines.append(f"Current spot: {current_spot:.2f}")
        body_lines.append(f"P&L delta:    {delta:+.1f} pts")
        if held_min is not None:
            body_lines.append(f"Held:         {held_min} min")
        body_lines.append("")
    body_lines.append("Action: close the position. Engine state has changed.")

    return _post(
        url, title, "\n".join(body_lines),
        priority="high",
        tags=["x" if current_state == "NEUTRAL" else "arrows_counterclockwise"],
    )


def send_info(title: str, message: str) -> bool:
    """Generic info-level notification (recorder started, error, etc.)."""
    url = _topic_url()
    if not url:
        return False
    return _post(url, title, message, priority="default", tags=["information_source"])


def send_test() -> bool:
    """One-shot test message — confirms the topic + phone subscription work."""
    url = _topic_url()
    if not url:
        print("NTFY_TOPIC not configured.")
        return False
    ok = _post(
        url,
        "Signals test alert",
        "If you see this, ntfy.sh is working. You'll get push alerts when NIFTY signals fire.",
        priority="default",
        tags=["white_check_mark"],
    )
    print("Test sent." if ok else "Test FAILED.")
    return ok


if __name__ == "__main__":
    send_test()
