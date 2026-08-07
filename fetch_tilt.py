#!/usr/bin/env python3
"""
Tilt Score fetcher (0DTE / nearest-expiration version).

Pulls delayed option chains from Cboe for each symbol, isolates the NEAREST
expiration INCLUDING the same-day (0DTE) one, and computes tilt on that
expiry's volume only.

Tilt = call volume / (call volume + put volume) * 100, nearest expiry only

This is a 0DTE/1DTE service: the same-day expiry is the point, so it is kept,
not skipped, Monday through Thursday. Friday is the one exception: same-day
is skipped there and the near view rolls to the next expiry (Monday) instead,
since a Friday-afternoon 0DTE read is stale by the time anyone reads it over
the weekend. Set EXPIRY_AFTER_TODAY = True to instead skip the same-day expiry
every day (the old behavior, which scored near-empty expiries and produced
noise on thinly-traded names).

Run once per day after the close (Cboe delayed data finalizes shortly after
4:15pm ET); the numbers are read the next morning as the prior session's
0DTE tilt. Scheduling examples:
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
    "AAPL", "AMZN", "AVGO", "GOOGL", "META", "MSFT", "NVDA",
    "TSLA", "AMD", "XLF", "INTC", "MU", "SMH", "GLD", "SLV", "TLT",
    "IBM", "WMT", "ORCL",
]

URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
# Output path defaults next to the script (GitHub Pages layout); on the droplet
# TILT_JSON points at the nginx-served data dir, outside the git checkout.
OUT = Path(os.environ.get("TILT_JSON") or (Path(__file__).resolve().parent / "tilt.json"))
OCC = re.compile(r"^[A-Z.^]+(\d{6})([CP])\d{8}$")
EXPIRY_AFTER_TODAY = False   # False = keep the same-day 0DTE (this is a 0DTE service)
VOLUME_FLOOR = 1000          # roll past any expiry trading fewer contracts than this
HISTORY_KEEP = 60
STALE_MINUTES = 45           # alert only when no symbol has refreshed in this long

# Returned when the fetch itself worked but the chain carries no volume yet. Cboe
# zeroes the session volume when its file rolls to the new session (~9:45am ET),
# so every symbol comes back empty for exactly one 15-min cycle each morning.
# That is not a failure: the prior row carries forward and the next run fills in.
EMPTY = object()

# Optional alerting, all no-ops when the env var is unset (so local runs stay
# quiet). On the droplet these come from /home/deploy/tiltscore/.env; the fetcher
# self-reports so it needs no GitHub Actions wrapper:
#   SLACK_WEBHOOK_URL - partial failures and stale data (from main) + crashes
#                       (from __main__).
#   HEALTHCHECK_URL   - a run that leaves the table fresh pings the URL, stale data
#                       pings URL + "/fail"; a missed ping trips the healthchecks.io
#                       dead-man's-switch.
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()


def ping_healthcheck(suffix: str = "") -> None:
    """Best-effort healthchecks.io ping. Never raises."""
    if not HEALTHCHECK_URL:
        return
    try:
        urllib.request.urlopen(HEALTHCHECK_URL.rstrip("/") + suffix, timeout=15).read()
    except Exception as e:
        print(f"  healthcheck ping failed: {e}", file=sys.stderr)


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


def age_minutes(iso: str | None, now: datetime) -> float | None:
    """Minutes since an ISO timestamp, or None if it is missing/unparseable."""
    if not iso:
        return None
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds() / 60


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
    now_local = datetime.now(timezone.utc).astimezone()
    today = now_local.strftime("%y%m%d")
    # Friday's same-day expiry is the last liquid session before the weekend gap,
    # so skip it and roll to Monday's instead. Mon-Thu keep 0DTE (Tue/Thu already
    # roll to Wed/Fri naturally below since those tickers have no Tue/Thu expiry).
    friday_skip_same_day = now_local.weekday() == 4

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

    if not by_exp:
        print(f"  {sym}: no contracts in the payload, keeping last good row")
        return EMPTY

    # Whole chain: every expiration summed (matches a standard put/call read).
    calls_all = sum(c for c, _ in by_exp.values())
    puts_all = sum(p for _, p in by_exp.values())
    total_all = calls_all + puts_all
    if total_all == 0:
        print(f"  {sym}: zero session volume (chain just rolled), keeping last good row")
        return EMPTY

    # Near-term: nearest expiry, same-day (0DTE) included unless EXPIRY_AFTER_TODAY
    # (or it's Friday, see friday_skip_same_day above), rolling past dead expiries
    # (e.g. GOOGL's ~40-contract Wednesday) to the first clearing the floor; if none
    # qualifies, take the heaviest upcoming one.
    exclude_today = EXPIRY_AFTER_TODAY or friday_skip_same_day
    live = sorted(e for e in by_exp if (e > today if exclude_today else e >= today))
    front = next((e for e in live if sum(by_exp[e]) >= VOLUME_FLOOR), None)
    if front is None and live:
        front = max(live, key=lambda e: sum(by_exp[e]))
    if front:
        cn, pn = by_exp[front]
        tn = cn + pn
        near = {"calls": cn, "puts": pn, "total": tn,
                "tilt": round(cn / tn * 100, 1) if tn else None}
        expiry = f"20{front[:2]}-{front[2:4]}-{front[4:]}"
    else:
        near = {"calls": 0, "puts": 0, "total": 0, "tilt": None}
        expiry = None

    return {
        "symbol": sym,
        "spot": data.get("current_price"),
        "spot_change_pct": data.get("price_change_percent"),
        "expiry": expiry,
        "near": near,
        "chain": {"calls": calls_all, "puts": puts_all, "total": total_all,
                  "tilt": round(calls_all / total_all * 100, 1)},
    }


def main() -> int:
    # Load prior file so history and last-known-good rows carry forward.
    prior_history: dict[str, list] = {}
    prior_rows: dict[str, dict] = {}
    if OUT.exists():
        try:
            prior = json.loads(OUT.read_text())
            prior_history = prior.get("history", {})
            prior_rows = {r["symbol"]: r for r in prior.get("rows", [])}
        except Exception:
            pass

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat(timespec="seconds")
    rows, failed, empty = [], [], []
    for sym in SYMBOLS:
        row = fetch_symbol(sym)
        if row is None or row is EMPTY:
            # `empty` = fetched fine, nothing to score yet (session rollover).
            # `failed` = the fetch itself broke. Only the latter is a problem.
            (empty if row is EMPTY else failed).append(sym)
            # Keep showing the last-known-good figures instead of dropping the
            # row from the page; the page's "last updated" reflects `updated`,
            # not this run's timestamp, so a stale row reads as stale, not fresh.
            stale = prior_rows.get(sym)
            if stale is not None:
                rows.append(stale)
            continue
        row["updated"] = now_iso
        rows.append(row)

        hist = [h for h in prior_history.get(sym, []) if h["date"] != today]
        hist.append({"date": today, "near": row["near"]["tilt"], "chain": row["chain"]["tilt"]})
        prior_history[sym] = hist[-HISTORY_KEEP:]

        # Day-over-day change per view, if we have a previous date.
        prev = [h for h in prior_history[sym] if h["date"] != today]
        p = prev[-1] if prev else {}
        row["near"]["tilt_prev"] = p.get("near", p.get("tilt"))   # old entries stored "tilt"
        row["chain"]["tilt_prev"] = p.get("chain")
        print(f"  {sym}: near {row['near']['tilt']} / chain {row['chain']['tilt']}")

    out = {
        "generated": now_iso,
        "source": "Cboe delayed quotes (15-min delay; intraday values are partial-day). near = nearest expiration incl. same-day 0DTE (Mon-Thu; Fridays roll to the next expiry), rolling past sub-1,000-contract expiries; chain = all expirations",
        "rows": rows,
        "failed": failed,
        "empty": empty,
        "history": prior_history,
    }
    OUT.write_text(json.dumps(out, indent=1))
    print(f"Wrote {OUT} ({len(rows)} symbols, {len(failed)} failed, {len(empty)} empty)")

    # Health is measured on the data, not on this one run: a cycle where every
    # symbol failed (or came back empty) is harmless as long as the carried-over
    # rows are recent. `freshest` is the age of the most recently updated row, so
    # a real outage trips the alert once the whole table has gone stale.
    ages = [a for a in (age_minutes(r.get("updated"), now_dt) for r in rows) if a is not None]
    freshest = min(ages) if ages else None
    healthy = freshest is not None and freshest <= STALE_MINUTES

    # Partial failure: the file still wrote and the job exits 0, so nobody sees
    # it unless we say so. A run where *every* symbol failed says nothing here -
    # one bad cycle carries forward harmlessly, and if it keeps up, the staleness
    # alert below fires instead of one Slack post per 15 minutes.
    if failed and healthy and len(failed) < len(SYMBOLS):
        link = run_url()
        msg = (f":warning: Tilt Score: {len(failed)} of {len(SYMBOLS)} symbols "
               f"failed to fetch ({', '.join(failed)}). Page updated with the rest.")
        notify_slack(msg + (f"\n{link}" if link else ""))

    if not healthy:
        link = run_url()
        age = f"in {int(freshest)} min" if freshest is not None else "at all"
        detail = ", ".join(x for x in (
            f"failed: {', '.join(failed)}" if failed else "",
            f"no volume: {', '.join(empty)}" if empty else "",
        ) if x)
        msg = (f":rotating_light: Tilt Score data is stale: no symbol has updated "
               f"{age}." + (f" ({detail})" if detail else ""))
        notify_slack(msg + (f"\n{link}" if link else ""))
        print(msg, file=sys.stderr)

    return 0 if healthy else 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        notify_slack(f":rotating_light: Tilt Score fetch crashed: {e}")
        ping_healthcheck("/fail")
        raise
    # main() already posted the Slack detail for an unhealthy run, so only the
    # ping is left here: healthy keeps the dead-man's-switch happy, stale trips it.
    ping_healthcheck("" if code == 0 else "/fail")
    sys.exit(code)
