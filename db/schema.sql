-- Schema for the Neon Postgres data layer (audit item 11).
-- Run once against a fresh Neon database: psql "$DATABASE_URL" -f db/schema.sql

CREATE TABLE IF NOT EXISTS decisions (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    cycle_id TEXT,                      -- was trace_id in decisions.jsonl
    step TEXT NOT NULL,
    symbol TEXT,
    input JSONB NOT NULL,
    output JSONB NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS decisions_ts_idx ON decisions (ts DESC);
CREATE INDEX IF NOT EXISTS decisions_cycle_id_idx ON decisions (cycle_id);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,                -- Alpaca order id
    decision_trace_id TEXT,
    client_order_id TEXT,
    symbol TEXT NOT NULL,
    strategy_type TEXT NOT NULL,
    legs JSONB NOT NULL,
    contracts INT NOT NULL,
    entry_price NUMERIC NOT NULL,
    max_loss NUMERIC NOT NULL,
    max_gain TEXT NOT NULL,             -- numeric or "unlimited" - stored as text, parsed by callers
    expiry DATE NOT NULL,
    expiry_mode TEXT NOT NULL DEFAULT 'weekly',  -- 'weekly' | '0dte' - doc/zerodte_and_watchlist.md item 1
    order_status TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
    entry_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    close_order_id TEXT,
    close_reason TEXT,
    exit_price NUMERIC,
    realized_pnl NUMERIC,
    outcome TEXT,                        -- 'WIN' | 'LOSS' | 'NEUTRAL'
    exit_ts TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS positions_status_idx ON positions (status);
CREATE INDEX IF NOT EXISTS positions_symbol_idx ON positions (symbol);

CREATE TABLE IF NOT EXISTS iv_history (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    date DATE NOT NULL,
    atm_iv NUMERIC NOT NULL,
    UNIQUE (symbol, date)               -- one row per symbol per real calendar day - see audit item 2
);

CREATE TABLE IF NOT EXISTS retrain_log (
    id BIGSERIAL PRIMARY KEY,
    val_loss NUMERIC,
    trained_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inference_spend (
    scope TEXT PRIMARY KEY,             -- e.g. 'featherless' - single running-total row per scope
    cumulative_total NUMERIC NOT NULL DEFAULT 0,
    request_count INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Portfolio-level state that isn't naturally one of the tables above but
-- currently lives in a local JSON file (data/equity_high_water_mark.json) -
-- included so the migration has nowhere left to fall back to a local file.
CREATE TABLE IF NOT EXISTS account_state (
    key TEXT PRIMARY KEY,               -- e.g. 'equity_high_water_mark'
    value NUMERIC NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration for tables created before doc/zerodte_and_watchlist.md - the
-- CREATE TABLE above only takes effect on a fresh database, so an existing
-- `positions` table needs this run explicitly. Idempotent, safe to re-run.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS expiry_mode TEXT NOT NULL DEFAULT 'weekly';
