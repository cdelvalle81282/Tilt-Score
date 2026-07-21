#!/usr/bin/env python3
"""
Tilt Score fetcher (front-expiration version).

Pulls delayed option chains from Cboe for each symbol, isolates the NEAREST
UPCOMING expiration (the first expiry strictly after the run date), and
computes tilt on that expiry's volume only.

Tilt = call volume / (call volume + put volume) * 100, front expiry only

Contracts expiring on the run date itself are excluded, so a Monday-evening
run on a Mon/Wed/Fri name scores Wednesday. To include same-day (0DTE)
volume instead, change EXPIRY_AFTER_TODAY to False.

Run once per day after the close (Cboe delayed data finalizes shortly after
4:15pm ET). Scheduling examples:
  cron (Linux/mac):   20 16 * * 1-5  cd /path/to/dir && python3 fetch_tilt.py
  Task Scheduler (Windows): daily 4:20 PM ET, action = python fetch_tilt.py

The script keeps a rolling per-symbol history (last 60 runs, one per date)
inside tilt.json so the page can show day-over-day change.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = [
    "AAPL", "AMZN", "AVGO", "IBIT", "GOOGL", "META", "MSFT", "NVDA",
    "TSLA", "AMD", "XLF", "INTC", "MU", "SMH", "GLD", "SLV", "TLT",
]

URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
OUT = Path(__file__).resolve().parent / "tilt.json"
OCC = re.compile(r"^[A-Z.^]+(\d{6})([CP])\d{8}$")
EXPIRY_AFTER_TODAY = True
HISTORY_KEEP = 60

# Optional alerting. Both are no-ops when the env var is unset, so local runs
# stay quiet. In CI these come from repo secrets of the same name.
#   SLACK_WEBHOOK_URL - partial-failure alerts posted from this script.
#   (Hard failures / crashes are alerted by the workflow's if:failure() step,
#    which also drives the healthchecks.io dead-man's-switch.)
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()


def run_url() -> str:
    """Link back to the GitHub Actions run, when this runs in CI."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else ""


def notify_slack(text: str) -> None:
    """Best-effort Slack post. Never raises: a broken alert must not fail the run."""
    if not SLACK_WEBHOOK:
        return
    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"  slack notify failed: {e}", file=sys.stderr)


def fetch_symbol(sym: str) -> dict | None:
    req = urllib.request.Request(
        URL.format(sym=sym), headers={"User-Agent": "tilt-score/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.load(r)
    except Exception as e:
        print(f"  {sym}: FAILED ({e})", file=sys.stderr)
        return None

    data = payload.get("data", {})
    today = datetime.now(timezone.utc).astimezone().strftime("%y%m%d")

    # Bucket volume by expiration.
    by_exp: dict[str, list[int]] = {}
    for o in data.get("options", []):
        m = OCC.match(o.get("option", ""))
        if not m:
            continue
        exp, cp = m.group(1), m.group(2)
        v = int(o.get("volume") or 0)
        bucket = by_exp.setdefault(exp, [0, 0])
        bucket[0 if cp == "C" else 1] += v

    # Front expiry: first one after (or on) the run date.
    live = sorted(e for e in by_exp if (e > today if EXPIRY_AFTER_TODAY else e >= today))
    if not live:
        return None
    front = live[0]
    calls, puts = by_exp[front]
    total = calls + puts
    return {
        "symbol": sym,
        "expiry": f"20{front[:2]}-{front[2:4]}-{front[4:]}",
        "calls": calls,
        "puts": puts,
        "total": total,
        "tilt": round(calls / total * 100, 1) if total else None,
        "spot": data.get("current_price"),
        "spot_change_pct": data.get("price_change_percent"),
    }


def main() -> int:
    # Load prior file so history carries forward.
    prior_history: dict[str, list] = {}
    if OUT.exists():
        try:
            prior = json.loads(OUT.read_text())
            prior_history = prior.get("history", {})
        except Exception:
            pass

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    rows, failed = [], []
    for sym in SYMBOLS:
        row = fetch_symbol(sym)
        if row is None:
            failed.append(sym)
            continue
        rows.append(row)

        hist = [h for h in prior_history.get(sym, []) if h["date"] != today]
        if row["tilt"] is not None:
            hist.append({"date": today, "tilt": row["tilt"], "total": row["total"]})
        prior_history[sym] = hist[-HISTORY_KEEP:]

        # Day-over-day change if we have a previous date.
        prev = [h for h in prior_history[sym] if h["date"] != today]
        row["tilt_prev"] = prev[-1]["tilt"] if prev else None
        print(f"  {sym}: tilt {row['tilt']}  ({row['calls']:,}C / {row['puts']:,}P)")

    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Cboe delayed quotes, front expiration only (15-min delay; intraday values are partial-day)",
        "rows": rows,
        "failed": failed,
        "history": prior_history,
    }
    OUT.write_text(json.dumps(out, indent=1))
    print(f"Wrote {OUT} ({len(rows)} symbols, {len(failed)} failed)")

    # Partial failure: the file still wrote and the job will exit 0, so nobody
    # sees it unless we say so. (All-fail returns 1 below and the workflow's
    # failure step alerts instead, so we don't double-post here.)
    if failed and rows:
        link = run_url()
        msg = (f":warning: Tilt Score: {len(failed)} of {len(SYMBOLS)} symbols "
               f"failed to fetch ({', '.join(failed)}). Page updated with the rest.")
        notify_slack(msg + (f"\n{link}" if link else ""))

    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
