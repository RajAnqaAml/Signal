"""Snapshot recorder. Writes /api/signals output to snapshots/YYYY-MM-DD.jsonl
every INTERVAL_SECONDS during NSE market hours (Mon-Fri 09:15-15:30 IST).

Usage:
    python recorder.py                              # default 5-min cadence
    python recorder.py --interval 600               # 10-min cadence
    python recorder.py --once                       # one snapshot now, exit
    python recorder.py --stop-after 15:35           # exit when clock passes 15:35 IST

Behavior:
    - Outside market hours: idles, polling clock once per minute until the gate opens.
    - During market hours: fetches via build_signals_payload(), appends one JSON line
      to snapshots/<today>.jsonl, sleeps for INTERVAL_SECONDS.
    - On any NSE fetch failure: logs the error, skips the snapshot, continues.
    - With --stop-after HH:MM: gracefully exits at the given IST clock time
      (useful for Task Scheduler so the process doesn't linger overnight).
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta

from app import IST, build_signals_payload, is_market_open

try:
    import db as _db  # Supabase layer; safe to import even without creds
except ImportError:
    _db = None

try:
    import notify as _notify  # ntfy.sh push; safe to import even without creds
except ImportError:
    _notify = None


NOTIFY_COOLDOWN_MIN = 30  # suppress same-direction re-push within this window


def _previous_signal(symbol: str) -> str:
    """Return the previous snapshot's signal for symbol ('CALL'/'PUT'/'NEUTRAL'/'').
    Used so we only push notifications on TRANSITIONS, not on every continuation.
    Returns '' if no prior snapshot or no DB.
    """
    if _db is None or not _db.is_configured():
        return ""
    cli = _db.client(service=False) or _db.client(service=True)
    if cli is None:
        return ""
    try:
        resp = (
            cli.table("snapshots")
            .select("signal")
            .eq("symbol", symbol)
            .order("ts", desc=True)
            .limit(2)
            .execute()
        )
        rows = resp.data or []
        # rows[0] is the one we just wrote; rows[1] is the previous
        if len(rows) >= 2:
            return rows[1].get("signal", "")
    except Exception:
        pass
    return ""


def _last_same_direction_push_age_min(symbol: str, direction: str):
    """Find the most recent snapshot with the same direction in the last
    NOTIFY_COOLDOWN_MIN minutes. Returns minutes-ago of that snap, or None if no
    such snap. We treat any same-direction snap within the window as evidence
    that a previous push already alerted on this trend — even through brief
    NEUTRAL gaps.
    """
    if _db is None or not _db.is_configured():
        return None
    cli = _db.client(service=False) or _db.client(service=True)
    if cli is None:
        return None
    cutoff = datetime.now(tz=IST) - timedelta(minutes=NOTIFY_COOLDOWN_MIN)
    try:
        resp = (
            cli.table("snapshots")
            .select("ts,signal")
            .eq("symbol", symbol)
            .eq("signal", direction)
            .gte("ts", cutoff.isoformat())
            .order("ts", desc=True)
            .limit(2)
            .execute()
        )
        rows = resp.data or []
        # rows[0] is the one we just wrote (same direction); look at rows[1]
        if len(rows) >= 2:
            prior_ts = datetime.fromisoformat(rows[1]["ts"])
            age_min = (datetime.now(tz=IST) - prior_ts).total_seconds() / 60
            return age_min
    except Exception:
        pass
    return None


def _maybe_notify(symbol: str, sig_block: dict):
    """Decide whether to push a notification.
    Gates (ALL must be true):
      1. signal is CALL or PUT (not NEUTRAL)
      2. previous snapshot was different signal (transition, not continuation)
      3. no same-direction push within the last NOTIFY_COOLDOWN_MIN minutes
         (prevents re-alerts on a continuing trend that briefly flickered to
         NEUTRAL — Thursday 2026-05-21 produced 3 PUT pushes that were really
         the same trade because of this)
    """
    if _notify is None or not _notify.is_configured():
        return
    direction = sig_block.get("signal", "NEUTRAL")
    if direction == "NEUTRAL":
        return
    prev = _previous_signal(symbol)
    if prev == direction:
        return  # continuation, silent

    # Cooldown check: even if this is a fresh transition, suppress if we already
    # pushed in this direction within the cooldown window
    age = _last_same_direction_push_age_min(symbol, direction)
    if age is not None and age <= NOTIFY_COOLDOWN_MIN:
        print(
            f"[notify] {symbol} {direction} suppressed: same-direction push "
            f"{age:.1f} min ago (cooldown {NOTIFY_COOLDOWN_MIN} min)",
            flush=True,
        )
        return

    # New entry or direction flip — alert
    row_like = {**sig_block, "spot_price": sig_block.get("entry")}
    ok = _notify.send_signal_alert(symbol, row_like)
    print(f"[notify] {symbol} {direction} (prev={prev or 'none'}) -> {'sent' if ok else 'FAIL'}", flush=True)

SNAPSHOT_DIR = "snapshots"


def write_jsonl(payload, when):
    """Local belt-and-braces JSONL write. Best-effort; never raises."""
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        path = os.path.join(SNAPSHOT_DIR, f"{when.strftime('%Y-%m-%d')}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
        return path
    except OSError:
        # Ephemeral filesystem (Render etc.) — skip silently
        return None


def take_one(now, force=False):
    if not force and not is_market_open(now):
        print(f"[{now.strftime('%H:%M:%S')}] Market closed — skipping snapshot (use --force to override)", flush=True)
        return
    payload = build_signals_payload(now, verbose=False)

    # Primary destination: Supabase (if configured)
    db_status = "no-db"
    if _db is not None and _db.is_configured():
        try:
            result = _db.insert_snapshot(payload)
            db_status = f"db+{result.get('inserted', 0)}"
        except Exception as e:
            print(f"[{now.strftime('%H:%M:%S')}] Supabase insert FAILED: {e}", flush=True)
            raise

    # Secondary: local JSONL fallback (no-op on ephemeral filesystems)
    path = write_jsonl(payload, now)

    n = payload["data"]["NIFTY"]["signal"]
    b = payload["data"]["BANKNIFTY"]["signal"]
    target = path or db_status
    print(
        f"[{now.strftime('%H:%M:%S')}] -> {target} | "
        f"NIFTY {n['signal']:7s} {n['confidence']:5.1f}% (spot {payload['data']['NIFTY']['spot']['price']}) | "
        f"BN {b['signal']:7s} {b['confidence']:5.1f}% (spot {payload['data']['BANKNIFTY']['spot']['price']})",
        flush=True,
    )

    # Push notifications for BOTH symbols. _maybe_notify enforces the
    # transition gate + 30-min same-direction cooldown independently per
    # symbol. BANKNIFTY was originally silenced; we re-enabled it after the
    # 3-day data review showed BN notifications had 100% hit rate on +75 pts
    # across 5 fires. notify.py already has BN-aware TARGET_PTS / SL_PTS /
    # STRIKE_STEP / LOT_SIZE so the phone alert sizes correctly for BN.
    try:
        _maybe_notify("NIFTY", n)
    except Exception as e:
        print(f"[notify] error: {e}", flush=True)
    try:
        _maybe_notify("BANKNIFTY", b)
    except Exception as e:
        print(f"[notify] error: {e}", flush=True)


def parse_stop_after(s):
    """Parse 'HH:MM' into (hour, minute) tuple, or return None."""
    if not s:
        return None
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except Exception:
        raise SystemExit(f"--stop-after must be HH:MM (got {s!r})")


def past_stop_time(now, stop_hm):
    if stop_hm is None:
        return False
    h, m = stop_hm
    return (now.hour, now.minute) >= (h, m)


def main():
    parser = argparse.ArgumentParser(description="NSE signal snapshot recorder")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between snapshots (default 300 = 5 min)")
    parser.add_argument("--once", action="store_true", help="Take one snapshot and exit. Honors market hours unless --force.")
    parser.add_argument("--force", action="store_true", help="Bypass market-hours gate (for local testing). Use with --once.")
    parser.add_argument("--stop-after", default=None, help="HH:MM IST — exit cleanly when current time passes this (e.g. 15:35)")
    args = parser.parse_args()

    if args.once:
        now = datetime.now(tz=IST)
        print(f"[Recorder] One-shot mode at {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        try:
            take_one(now, force=args.force)
        except Exception as e:
            traceback.print_exc()
            print(f"[Recorder] ERROR: {e}")
        return

    stop_hm = parse_stop_after(args.stop_after)
    extra = f", stop-after={args.stop_after}" if stop_hm else ""
    print(f"[Recorder] Started at {datetime.now(tz=IST).strftime('%Y-%m-%d %H:%M:%S %Z')}, interval={args.interval}s{extra}",
          flush=True)
    while True:
        now = datetime.now(tz=IST)
        if past_stop_time(now, stop_hm):
            print(f"[{now.strftime('%H:%M:%S')}] Past stop-after {args.stop_after} — exiting cleanly", flush=True)
            sys.exit(0)
        if not is_market_open(now):
            print(f"[{now.strftime('%H:%M:%S')}] Market closed — idling 60s", flush=True)
            time.sleep(60)
            continue
        try:
            take_one(now)
            sys.stdout.flush()
        except Exception as e:
            print(f"[{now.strftime('%H:%M:%S')}] ERROR: {e}", flush=True)
            traceback.print_exc()
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
