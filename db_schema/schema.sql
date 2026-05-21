-- Supabase schema for the NSE signal recorder.
-- Apply by pasting into Supabase Dashboard → SQL Editor → New query → Run.
--
-- This is idempotent: DROPs first, then CREATEs. Safe to re-run when iterating.

DROP TABLE IF EXISTS snapshots CASCADE;
DROP TABLE IF EXISTS historical_candles CASCADE;

-- snapshots: one row per (timestamp, symbol) — replaces snapshots/*.jsonl
CREATE TABLE snapshots (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    spot_price NUMERIC,
    spot_change_pct NUMERIC,
    spot_open NUMERIC,
    spot_high NUMERIC,
    spot_low NUMERIC,
    spot_prev_close NUMERIC,
    signal TEXT NOT NULL,
    confidence NUMERIC,
    score NUMERIC,
    trend_score NUMERIC,
    oi_score NUMERIC,
    gap_weight NUMERIC,
    evidence_quality TEXT,
    entry NUMERIC,
    target1 NUMERIC,
    target2 NUMERIC,
    stop_loss NUMERIC,
    reasons JSONB,
    oi JSONB,
    option_chain JSONB,
    indicators JSONB,
    vix NUMERIC,
    vix_change NUMERIC,
    breadth JSONB,
    raw_payload JSONB,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(ts, symbol)
);

CREATE INDEX idx_snapshots_ts ON snapshots(ts DESC);
CREATE INDEX idx_snapshots_symbol_ts ON snapshots(symbol, ts DESC);
CREATE INDEX idx_snapshots_signal ON snapshots(signal) WHERE signal <> 'NEUTRAL';

-- historical_candles: 5-min OHLC for backtests — replaces history/*.json from Yahoo
CREATE TABLE historical_candles (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open NUMERIC,
    high NUMERIC,
    low NUMERIC,
    close NUMERIC,
    volume BIGINT,
    interval_minutes INTEGER NOT NULL DEFAULT 5,
    source TEXT NOT NULL DEFAULT 'yahoo',
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(symbol, ts, interval_minutes)
);

CREATE INDEX idx_candles_symbol_ts ON historical_candles(symbol, ts DESC);

-- Row Level Security: anon role gets read-only access; writes require service role.
ALTER TABLE snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE historical_candles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon read snapshots"
    ON snapshots FOR SELECT
    TO anon
    USING (true);

CREATE POLICY "anon read candles"
    ON historical_candles FOR SELECT
    TO anon
    USING (true);

-- Service role bypasses RLS entirely (default behavior in Supabase), so no policy needed for writes.
-- If you ever expose anon writes, ADD a restrictive policy here.
