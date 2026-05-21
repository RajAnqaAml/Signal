"""Supabase database layer for the NSE signal recorder.

Design rules:
- Importing this module never raises, even if credentials are missing — local
  development works without Supabase configured. `client()` returns None when
  env vars are absent, and callers must check for that.
- Writes go through the SERVICE role key (bypasses RLS). Reads can use anon key.
- Idempotent inserts via ON CONFLICT — replays of the same snapshot are no-ops.

Env vars (loaded from .env via python-dotenv if present):
    SUPABASE_URL          required
    SUPABASE_SERVICE_KEY  required for writes (recorder, backfill)
    SUPABASE_ANON_KEY     required for read-only clients (frontend uses this separately)
"""
import json
import os
from datetime import datetime
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_client_cache = {}


def _supabase_client(service: bool):
    """Return a cached supabase-py client, or None if creds are missing."""
    key_name = "SUPABASE_SERVICE_KEY" if service else "SUPABASE_ANON_KEY"
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get(key_name)
    if not url or not key:
        return None
    cache_key = ("service" if service else "anon")
    if cache_key in _client_cache:
        return _client_cache[cache_key]
    try:
        from supabase import create_client
    except ImportError:
        return None
    cli = create_client(url, key)
    _client_cache[cache_key] = cli
    return cli


def client(service: bool = True):
    """Public entrypoint. Returns Supabase client or None if not configured."""
    return _supabase_client(service)


def is_configured() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


# ─── Timestamp helpers ──────────────────────────────────────────────────────
def _parse_payload_ts(ts_str: str) -> datetime:
    """Parse '2026-05-20 10:01:46 IST' -> aware datetime in IST."""
    cleaned = ts_str.replace(" IST", "").strip()
    return datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)


def _iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat()


# ─── snapshots ──────────────────────────────────────────────────────────────
def _row_from_payload(payload: dict, symbol: str) -> Optional[dict]:
    """Flatten one symbol's slice of build_signals_payload output into a DB row."""
    block = payload.get("data", {}).get(symbol)
    if not block:
        return None
    spot = block.get("spot") or {}
    sig = block.get("signal") or {}
    ts = _parse_payload_ts(payload["timestamp"])
    vix = payload.get("vix") or {}
    return {
        "ts": _iso(ts),
        "symbol": symbol,
        "spot_price": spot.get("price"),
        "spot_change_pct": spot.get("change"),
        "spot_open": spot.get("open"),
        "spot_high": spot.get("high"),
        "spot_low": spot.get("low"),
        "spot_prev_close": spot.get("prev_close"),
        "signal": sig.get("signal", "NEUTRAL"),
        "confidence": sig.get("confidence"),
        "score": sig.get("score"),
        "trend_score": sig.get("trend_score"),
        "oi_score": sig.get("oi_score"),
        "gap_weight": sig.get("gap_weight"),
        "evidence_quality": sig.get("evidence_quality"),
        "entry": sig.get("entry"),
        "target1": sig.get("target1"),
        "target2": sig.get("target2"),
        "stop_loss": sig.get("stop_loss"),
        "reasons": sig.get("reasons"),
        "oi": block.get("oi"),
        "option_chain": block.get("option_chain"),
        "indicators": block.get("indicators"),
        "vix": vix.get("value"),
        "vix_change": vix.get("change"),
        "breadth": block.get("breadth"),
        "raw_payload": block,  # full per-symbol block for debugging
    }


def insert_snapshot(payload: dict) -> dict:
    """Insert NIFTY + BANKNIFTY rows from one build_signals_payload output.
    Returns dict with counts. Idempotent — duplicates on (ts, symbol) are skipped.
    No-op if Supabase isn't configured.
    """
    cli = client(service=True)
    if cli is None:
        return {"skipped": True, "reason": "supabase not configured"}

    rows = []
    for sym in ("NIFTY", "BANKNIFTY"):
        row = _row_from_payload(payload, sym)
        if row:
            rows.append(row)
    if not rows:
        return {"inserted": 0}

    # upsert with on_conflict on the UNIQUE (ts, symbol) constraint
    try:
        resp = cli.table("snapshots").upsert(
            rows, on_conflict="ts,symbol", ignore_duplicates=True
        ).execute()
        return {"inserted": len(resp.data) if resp.data else 0, "submitted": len(rows)}
    except Exception as e:
        # Re-raise so the recorder cron exits non-zero on persistent failure
        raise RuntimeError(f"Supabase insert_snapshot failed: {e}") from e


def get_snapshots(date_str: str, symbol: str) -> list:
    """Fetch all snapshots for one date (YYYY-MM-DD IST) + symbol, ordered ascending."""
    cli = client(service=False) or client(service=True)
    if cli is None:
        return []
    # IST day window -> UTC bounds
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    end = start.replace(hour=23, minute=59, second=59)
    resp = (
        cli.table("snapshots")
        .select("*")
        .eq("symbol", symbol)
        .gte("ts", start.isoformat())
        .lte("ts", end.isoformat())
        .order("ts", desc=False)
        .execute()
    )
    return resp.data or []


def latest_snapshot(symbol: str) -> Optional[dict]:
    """Most recent snapshot for a symbol, or None."""
    cli = client(service=False) or client(service=True)
    if cli is None:
        return None
    resp = (
        cli.table("snapshots")
        .select("*")
        .eq("symbol", symbol)
        .order("ts", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


# ─── historical_candles ─────────────────────────────────────────────────────
def insert_candle_batch(symbol: str, candles: list, interval: int = 5, source: str = "yahoo") -> dict:
    """Bulk insert 5-min OHLC candles. Each entry has unix `ts`, open/high/low/close/volume."""
    cli = client(service=True)
    if cli is None:
        return {"skipped": True, "reason": "supabase not configured"}

    rows = []
    for c in candles:
        if c.get("close") is None:
            continue
        ts_unix = c.get("ts")
        if ts_unix is None:
            continue
        dt = datetime.fromtimestamp(ts_unix, tz=IST)
        rows.append({
            "symbol": symbol,
            "ts": dt.isoformat(),
            "open": c.get("open"),
            "high": c.get("high"),
            "low": c.get("low"),
            "close": c.get("close"),
            "volume": c.get("volume", 0),
            "interval_minutes": interval,
            "source": source,
        })
    if not rows:
        return {"inserted": 0}

    # Supabase has request size limits; chunk in 500s
    inserted_total = 0
    CHUNK = 500
    for i in range(0, len(rows), CHUNK):
        batch = rows[i : i + CHUNK]
        resp = cli.table("historical_candles").upsert(
            batch, on_conflict="symbol,ts,interval_minutes", ignore_duplicates=True
        ).execute()
        inserted_total += len(resp.data) if resp.data else 0
    return {"inserted": inserted_total, "submitted": len(rows)}


def get_candles(symbol: str, start_date: str = None, end_date: str = None,
                interval: int = 5) -> list:
    """Fetch candles for a symbol, optionally filtered to a date range (IST dates).
    Paginates around Supabase's 1000-row default cap.
    """
    cli = client(service=False) or client(service=True)
    if cli is None:
        return []
    PAGE = 1000
    out = []
    offset = 0
    while True:
        q = (
            cli.table("historical_candles")
            .select("*")
            .eq("symbol", symbol)
            .eq("interval_minutes", interval)
            .order("ts", desc=False)
            .range(offset, offset + PAGE - 1)
        )
        if start_date:
            start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=IST)
            q = q.gte("ts", start.isoformat())
        if end_date:
            end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=IST)
            q = q.lte("ts", end.isoformat())
        resp = q.execute()
        rows = resp.data or []
        out.extend(rows)
        if len(rows) < PAGE:
            break
        offset += PAGE
    return out


# ─── CLI for sanity checks ──────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if not is_configured():
        print("Supabase NOT configured (SUPABASE_URL / SUPABASE_SERVICE_KEY missing).")
        print("Set them in .env (see .env.example) and re-run.")
        sys.exit(1)
    print(f"Configured: {os.environ['SUPABASE_URL']}")
    cli = client(service=True)
    try:
        resp = cli.table("snapshots").select("id", count="exact").limit(0).execute()
        print(f"snapshots count: {resp.count}")
        resp = cli.table("historical_candles").select("id", count="exact").limit(0).execute()
        print(f"historical_candles count: {resp.count}")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
