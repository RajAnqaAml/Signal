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

try:
    import ai_filter as _ai_filter
    import market_context as _mctx
    _AI_ENABLED = True
except ImportError:
    _AI_ENABLED = False


NOTIFY_COOLDOWN_MIN = 0  # no cooldown — push on every signal transition


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


def _last_push_age_min(symbol: str, direction: str):
    """Find when the most recent PUSH for this symbol+direction actually fired.
    A 'push moment' is a snapshot where signal == direction AND the immediately
    preceding snapshot had a DIFFERENT signal (i.e., the moment the engine flipped
    INTO this direction). We need to walk the snapshot sequence to detect this.

    Returns minutes-ago of the most recent push (excluding the snap just written),
    or None if there's no prior same-direction push in the lookback window.

    Why this is better than the previous logic:
      The old function checked "any same-direction snap in last 30 min" -- which
      meant a continuous 3-hour CALL streak that briefly flickered to NEUTRAL
      and came back to CALL would be SUPPRESSED because a same-direction snap
      existed 5 min ago. But that suppression hides a legitimate re-entry
      transition. We saw this on 2026-05-25 BANKNIFTY: 3-hour CALL streak ->
      NEUTRAL for 1 snap -> CALL again at 12:10. The re-entry push got
      suppressed, which the user wanted to receive.

      The new function only suppresses if a *transition* (NEUTRAL/PUT -> CALL
      or NEUTRAL/CALL -> PUT) happened within the cooldown. Continuations
      don't count toward cooldown.
    """
    if _db is None or not _db.is_configured():
        return None
    cli = _db.client(service=False) or _db.client(service=True)
    if cli is None:
        return None
    try:
        # Pull last ~40 snaps -- enough to span ~3h at 5-min cadence so we don't
        # miss recent transitions even after a long continuation streak.
        resp = (
            cli.table("snapshots")
            .select("ts,signal")
            .eq("symbol", symbol)
            .order("ts", desc=True)
            .limit(40)
            .execute()
        )
        rows = resp.data or []
        if len(rows) < 3:
            return None

        # Order chronologically and exclude the most recent snap (the one we
        # just wrote -- which is itself a transition we're evaluating).
        rows_chrono = list(reversed(rows))[:-1]

        # Find the most recent transition INTO `direction`
        last_transition_ts = None
        for i in range(1, len(rows_chrono)):
            cur = rows_chrono[i].get("signal")
            prev = rows_chrono[i - 1].get("signal")
            if cur == direction and prev != direction:
                last_transition_ts = datetime.fromisoformat(rows_chrono[i]["ts"])

        if last_transition_ts is None:
            return None
        age_min = (datetime.now(tz=IST) - last_transition_ts).total_seconds() / 60
        return age_min
    except Exception:
        pass
    return None


def _last_active_direction_entry(symbol: str, since_min: int = 240):
    """Find the most recent ENTRY push (a transition from NEUTRAL or opposite
    to CALL/PUT) within the last `since_min` minutes. Returns (direction,
    entry_ts, entry_spot) or None.
    Used to detect "exit moments" — when engine flips out of an active trade.
    """
    if _db is None or not _db.is_configured():
        return None
    cli = _db.client(service=False) or _db.client(service=True)
    if cli is None:
        return None
    cutoff = datetime.now(tz=IST) - timedelta(minutes=since_min)
    try:
        resp = (
            cli.table("snapshots")
            .select("ts,signal,spot_price")
            .eq("symbol", symbol)
            .gte("ts", cutoff.isoformat())
            .order("ts", desc=True)
            .limit(60)
            .execute()
        )
        rows = list(reversed(resp.data or []))
        if len(rows) < 3:
            return None
        # Walk backward from most recent, find the most recent transition INTO
        # a direction (i.e., signal[i] in {CALL,PUT} and signal[i-1] is different).
        for i in range(len(rows) - 1, 0, -1):
            cur = rows[i].get("signal")
            prev = rows[i - 1].get("signal")
            if cur in ("CALL", "PUT") and prev != cur:
                return cur, datetime.fromisoformat(rows[i]["ts"]), rows[i]["spot_price"]
        return None
    except Exception:
        return None


def _maybe_notify(symbol: str, sig_block: dict, current_spot: float = None):
    """Decide whether to push a notification.

    Three kinds of pushes:
      A. ENTRY push: signal flips from NEUTRAL to CALL/PUT (transition).
         Requires Tier 1 + cooldown + transition gate.
      B. DIRECTION FLIP push: CALL->PUT or PUT->CALL. Always alert immediately.
      C. EXIT push: signal flips from CALL/PUT to NEUTRAL after we had an
         active trade. Tells user to close the position.

    V3 changes vs prior version:
      - Only push TIER_1 signals (Tier 2 = dashboard-only WATCH).
      - Add exit notifications when engine flips back to NEUTRAL.
    """
    if _notify is None or not _notify.is_configured():
        return

    direction = sig_block.get("signal", "NEUTRAL")
    prev = _previous_signal(symbol)
    push_tier = sig_block.get("push_tier", "TIER_3")
    tier_blocks = sig_block.get("tier_blocks", [])

    # ── EXIT PUSH: prev was CALL/PUT, current is NEUTRAL ─────────────
    if direction == "NEUTRAL" and prev in ("CALL", "PUT"):
        last_entry = _last_active_direction_entry(symbol)
        if last_entry and last_entry[0] == prev:
            entry_dir, entry_ts, entry_spot = last_entry
            held_min = int((datetime.now(tz=IST) - entry_ts).total_seconds() / 60)
            ok = _notify.send_exit_alert(
                symbol, prior_direction=entry_dir, current_state="NEUTRAL",
                entry_spot=entry_spot, current_spot=current_spot, held_min=held_min,
            )
            print(
                f"[notify] {symbol} EXIT ({entry_dir} -> NEUTRAL after {held_min} min) "
                f"-> {'sent' if ok else 'FAIL'}",
                flush=True,
            )
        return

    # ── ENTRY/FLIP push: only if signal is non-NEUTRAL ─────────────
    if direction == "NEUTRAL":
        return  # nothing to push for NEUTRAL with no prior trade
    if prev == direction:
        return  # continuation, silent

    # Tier gate: only TIER_1 signals push to phone (Tier 2 -> WATCH on dashboard).
    if push_tier != "TIER_1":
        print(
            f"[notify] {symbol} {direction} suppressed: push_tier={push_tier} "
            f"(blocks: {'; '.join(tier_blocks[:2])})",
            flush=True,
        )
        return

    # Cooldown check: same-direction push fired within cooldown window?
    # Direction flips bypass cooldown (CALL->PUT is always urgent).
    if prev != ("PUT" if direction == "CALL" else "CALL"):
        age = _last_push_age_min(symbol, direction)
        if age is not None and age <= NOTIFY_COOLDOWN_MIN:
            print(
                f"[notify] {symbol} {direction} suppressed: prior push fired "
                f"{age:.1f} min ago (cooldown {NOTIFY_COOLDOWN_MIN} min)",
                flush=True,
            )
            return

    # ── AI Filter (Gemini with Google Search grounding) ────────────────
    # Uses ai_result attached to sig_block by take_one() before calling here.
    ai = sig_block.get("_ai_result")
    if ai:
        if ai["verdict"] == "SKIP":
            print(
                f"[ai_filter] {symbol} {direction} SKIPPED by AI: {ai['reason']}",
                flush=True,
            )
            return
        # Attach AI verdict to the notification row
        sig_block = {**sig_block,
                     "ai_verdict":  ai["verdict"],
                     "ai_risk":     ai["risk"],
                     "ai_reason":   ai["reason"],
                     "ai_concern":  ai["key_concern"]}

    # Fire entry push
    row_like = {**sig_block, "spot_price": sig_block.get("entry")}
    ok = _notify.send_signal_alert(symbol, row_like)
    ai_tag = f" [AI:{ai['verdict']}/{ai['risk']}]" if ai else ""
    print(f"[notify] {symbol} {direction} TIER_1 (prev={prev or 'none'}){ai_tag} -> {'sent' if ok else 'FAIL'}", flush=True)

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
    sx_part = ""
    if "SENSEX" in payload["data"]:
        sx = payload["data"]["SENSEX"]["signal"]
        sx_part = f" | SX {sx['signal']:7s} {sx['confidence']:5.1f}% (spot {payload['data']['SENSEX']['spot']['price']})"
    target = path or db_status
    print(
        f"[{now.strftime('%H:%M:%S')}] -> {target} | "
        f"NIFTY {n['signal']:7s} {n['confidence']:5.1f}% (spot {payload['data']['NIFTY']['spot']['price']}) | "
        f"BN {b['signal']:7s} {b['confidence']:5.1f}% (spot {payload['data']['BANKNIFTY']['spot']['price']})"
        f"{sx_part}",
        flush=True,
    )

    # Push notifications for all symbols. V3: only TIER_1 signals push to
    # phone; TIER_2 surfaces on dashboard only. _maybe_notify also fires EXIT
    # pushes when engine flips out of an active direction.
    #
    # AI Filter: for every TIER_1 non-NEUTRAL entry signal, we call Gemini
    # (with Google Search grounding) before pushing. Result is attached to the
    # signal block as '_ai_result'. SKIP verdict suppresses the notification.
    nifty_spot = payload["data"]["NIFTY"]["spot"]["price"]
    bn_spot = payload["data"]["BANKNIFTY"]["spot"]["price"]

    _sym_spots = {
        "NIFTY":     nifty_spot,
        "BANKNIFTY": bn_spot,
    }
    if "SENSEX" in payload["data"]:
        _sym_spots["SENSEX"] = payload["data"]["SENSEX"]["spot"]["price"]

    for _sym, _sig, _spot in [
        ("NIFTY",     n,    nifty_spot),
        ("BANKNIFTY", b,    bn_spot),
    ] + ([("SENSEX", payload["data"]["SENSEX"]["signal"],
           payload["data"]["SENSEX"]["spot"]["price"])]
         if "SENSEX" in payload["data"] else []):
        try:
            # Run AI filter only on TIER_1 entry signals (saves API quota)
            if (_AI_ENABLED
                    and _sig.get("signal") != "NEUTRAL"
                    and _sig.get("push_tier") == "TIER_1"):
                try:
                    _ctx = _mctx.build_context(_sym, _sig, payload)
                    _ai  = _ai_filter.evaluate_signal(_sym, _sig, _ctx)
                    _sig["_ai_result"] = _ai
                except Exception as _ae:
                    print(f"[ai_filter] {_sym} error: {_ae} — skipping filter", flush=True)

            _maybe_notify(_sym, _sig, current_spot=_spot)
        except Exception as e:
            print(f"[notify] {_sym} error: {e}", flush=True)


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
