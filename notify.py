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
LOT_SIZE = {"NIFTY": 75, "BANKNIFTY": 15}
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
TARGET_PTS = {"NIFTY": 30, "BANKNIFTY": 60}
SL_PTS = {"NIFTY": 15, "BANKNIFTY": 30}
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

    # Body — compact, scannable
    body = [
        f"Spot:    {spot:.2f}",
        f"Buy:     {option}  (ATM weekly, {lot}-share lot)",
        "",
        f"Target:  spot {target_spot:.0f}  ({direction == 'PUT' and '-' or '+'}{target_pts} pts)  ≈ +Rs {target_inr}",
        f"Stop:    spot {sl_spot:.0f}  ({direction == 'PUT' and '+' or '-'}{sl_pts} pts)  ≈ -Rs {sl_inr}",
        f"Time:    exit in 10 min if neither hits",
        "",
        f"Score: {score:+.2f}  Conf: {conf:.0f}%  Tier: {tier}",
        "",
        "Why:",
    ]
    # Top 4 most useful reasons
    keep = [r for r in reasons if "Real intraday" not in r and "below threshold" not in r][:4]
    for r in keep:
        body.append(f"  - {r}")

    # Strip arrows so the message is robust across ntfy clients
    body_text = "\n".join(body).replace("→", "->").replace("—", "-")

    priority = "high"
    tags = ["chart_with_downwards_trend"] if direction == "PUT" else ["chart_with_upwards_trend"]
    return _post(url, title, body_text, priority=priority, tags=tags)


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
