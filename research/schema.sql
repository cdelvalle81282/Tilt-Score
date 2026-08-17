-- Tilt Score research store: one row per (symbol, trading date).
-- Separate from tilt.json (a live-snapshot file the page reads) -- this is an
-- append-only analysis dataset, meant to accumulate for months before any
-- per-symbol conclusion is trusted (see CLAUDE.md-adjacent discussion: don't
-- read into this before ~6 months / 125+ trading days per symbol).

CREATE TABLE IF NOT EXISTS tilt_daily (
    date            TEXT    NOT NULL,   -- YYYY-MM-DD, America/New_York trading day
    symbol          TEXT    NOT NULL,

    near_tilt       REAL,               -- near-expiry call volume %  (0-100)
    chain_tilt      REAL,               -- whole-chain call volume %  (0-100)
    spread          REAL,               -- near_tilt - chain_tilt

    near_volume     INTEGER,            -- total near-expiry contracts (calls+puts)
    chain_volume    INTEGER,            -- total chain contracts (calls+puts)

    close           REAL,               -- that day's close
    next_close      REAL,               -- next trading day's close
    fwd_return_pct  REAL,               -- (next_close - close) / close * 100
    fwd_direction   INTEGER,            -- 1 up / -1 down / 0 flat (|fwd_return_pct| < 0.05)

    had_earnings    INTEGER,            -- 1/0/NULL -- confound flag, not backfilled yet
    source          TEXT,               -- 'backfill' | 'daily' -- how the row was written

    PRIMARY KEY (date, symbol)
);

-- Convenience view: only rows with a usable forward-return label, so ad hoc
-- correlation queries don't have to repeat the NULL-filtering every time.
CREATE VIEW IF NOT EXISTS tilt_daily_labeled AS
SELECT * FROM tilt_daily
WHERE fwd_return_pct IS NOT NULL
  AND (near_tilt IS NOT NULL OR chain_tilt IS NOT NULL);
