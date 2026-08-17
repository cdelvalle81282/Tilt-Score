#!/usr/bin/env python3
"""
Backfill research/tilt_history.db from tilt.json's rolling `history[]` +
daily close prices, so tilt readings have a forward-return label to test
against.

Usage:
    python research/backfill_tilt_history.py [--tilt-json ../tilt.json] [--db tilt_history.db]

Requires: yfinance (pip install yfinance). Not a dependency of fetch_tilt.py
itself -- this is an offline research script, not part of the live pipeline.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

HERE = Path(__file__).parent


def load_history(tilt_json_path: Path) -> dict[str, list[dict]]:
    data = json.loads(tilt_json_path.read_text())
    return data.get("history", {})


def normalize_entries(symbol: str, entries: list[dict]) -> list[dict]:
    """Old format: {date, tilt, total}. New format: {date, near, chain[, near_volume,
    chain_volume]} -- the volume keys were only added to fetch_tilt.py's history
    write on 2026-08-16, so entries written before that date won't have them."""
    rows = []
    for e in entries:
        if "near" in e or "chain" in e:
            near_tilt, chain_tilt = e.get("near"), e.get("chain")
            near_volume, chain_volume = e.get("near_volume"), e.get("chain_volume")
        else:
            near_tilt, chain_tilt = e.get("tilt"), None
            near_volume, chain_volume = e.get("total"), None
        spread = (near_tilt - chain_tilt) if (near_tilt is not None and chain_tilt is not None) else None
        rows.append({
            "date": e["date"], "symbol": symbol,
            "near_tilt": near_tilt, "chain_tilt": chain_tilt, "spread": spread,
            "near_volume": near_volume, "chain_volume": chain_volume,
        })
    return rows


def fetch_closes(symbols: list[str], start: str, end: str) -> dict[str, dict[str, float]]:
    """Returns {symbol: {date_str: close}}. `end` is exclusive in yfinance, so
    callers should pass a few days past the last date they need a close for,
    to have a next_close for the final backfilled date."""
    closes: dict[str, dict[str, float]] = {}
    data = yf.download(symbols, start=start, end=end, progress=False, group_by="ticker",
                        auto_adjust=False)
    for sym in symbols:
        try:
            series = data[sym]["Close"].dropna()
        except (KeyError, IndexError):
            closes[sym] = {}
            continue
        closes[sym] = {ts.strftime("%Y-%m-%d"): float(v) for ts, v in series.items()}
    return closes


def attach_forward_returns(rows: list[dict], closes_by_symbol: dict[str, dict[str, float]]) -> None:
    for sym in {r["symbol"] for r in rows}:
        sym_closes = closes_by_symbol.get(sym, {})
        trading_days = sorted(sym_closes)
        sym_rows = sorted((r for r in rows if r["symbol"] == sym), key=lambda r: r["date"])
        for r in sym_rows:
            close = sym_closes.get(r["date"])
            r["close"] = close
            if close is None:
                r["next_close"] = r["fwd_return_pct"] = r["fwd_direction"] = None
                continue
            later = [d for d in trading_days if d > r["date"]]
            next_close = sym_closes[later[0]] if later else None
            r["next_close"] = next_close
            if next_close is None:
                r["fwd_return_pct"] = r["fwd_direction"] = None
            else:
                pct = (next_close - close) / close * 100
                r["fwd_return_pct"] = round(pct, 4)
                r["fwd_direction"] = 0 if abs(pct) < 0.05 else (1 if pct > 0 else -1)


def upsert(db_path: Path, rows: list[dict]) -> None:
    schema = (HERE / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    conn.executemany("""
        INSERT INTO tilt_daily (date, symbol, near_tilt, chain_tilt, spread,
                                 near_volume, chain_volume, close, next_close,
                                 fwd_return_pct, fwd_direction, had_earnings, source)
        VALUES (:date, :symbol, :near_tilt, :chain_tilt, :spread,
                :near_volume, :chain_volume, :close, :next_close,
                :fwd_return_pct, :fwd_direction, NULL, 'backfill')
        ON CONFLICT(date, symbol) DO UPDATE SET
            near_tilt=excluded.near_tilt, chain_tilt=excluded.chain_tilt,
            spread=excluded.spread, near_volume=excluded.near_volume,
            chain_volume=excluded.chain_volume, close=excluded.close,
            next_close=excluded.next_close, fwd_return_pct=excluded.fwd_return_pct,
            fwd_direction=excluded.fwd_direction
    """, rows)
    conn.commit()
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tilt-json", type=Path, default=HERE.parent / "tilt.json")
    ap.add_argument("--db", type=Path, default=HERE / "tilt_history.db")
    args = ap.parse_args()

    history = load_history(args.tilt_json)
    if not history:
        print(f"No history[] found in {args.tilt_json}")
        return

    rows = [row for sym, entries in history.items() for row in normalize_entries(sym, entries)]
    dates = sorted({r["date"] for r in rows})
    start = dates[0]
    end = (datetime.strptime(dates[-1], "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")

    print(f"Backfilling {len(rows)} (symbol, date) rows across {len(history)} symbols, {start} to {dates[-1]}")
    closes = fetch_closes(sorted(history), start, end)
    attach_forward_returns(rows, closes)

    labeled = sum(1 for r in rows if r["fwd_return_pct"] is not None)
    print(f"{labeled}/{len(rows)} rows got a forward-return label (rest are missing a close, "
          f"usually today's row with no next trading day yet)")

    upsert(args.db, rows)
    print(f"Wrote {args.db}")


if __name__ == "__main__":
    main()
