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
inside tilt.json so the page can show day-over-day change. That window drops
its oldest day once it is full, so it is not a research record: when
TILT_ARCHIVE_DIR is set, every run is also appended to a permanent per-day
file there (see archive_run) for backtesting.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYMBOLS = [
    "AAPL", "AMZN", "AVGO", "GOOGL", "META", "MSFT", "NVDA",
    "TSLA", "AMD", "XLF", "INTC", "MU", "SMH", "GLD", "SLV", "TLT", "SOXL",
    "EEM", "IBIT", "XLE", "TQQQ", "DRAM",
    "IBM", "WMT", "ORCL",
]

URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
# Output path defaults next to the script (GitHub Pages layout); on the droplet
# TILT_JSON points at the nginx-served data dir, outside the git checkout.
OUT = Path(os.environ.get("TILT_JSON") or (Path(__file__).resolve().parent / "tilt.json"))
OCC = re.compile(r"^[A-Z.^]+(\d{6})([CP])(\d{8})$")
EXPIRY_AFTER_TODAY = False   # False = keep the same-day 0DTE (this is a 0DTE service)
VOLUME_FLOOR = 1000          # roll past any expiry trading fewer contracts than this
HISTORY_KEEP = 60
STALE_MINUTES = 45           # alert only when no symbol has refreshed in this long
# Permanent research archive, off when unset (local runs write nothing). On the
# droplet it points outside both the git checkout and the nginx web root.
ARCHIVE_DIR = os.environ.get("TILT_ARCHIVE_DIR", "").strip()

# Stale-feed check. A fetch can succeed and still hand back the PRIOR session's
# volume: every morning before Cboe rolls its file (~9:45am ET), all day on a
# market holiday, and on 2026-09-23 when Cboe served the 09/22 file all session
# while every run here reported 25 ok. Each row's `session` (see fetch_symbol)
# says which day its volume was traded on; past FEED_DUE_ET on a trading day it
# must be today. 10:00am is the first run with real volume, 10:15 leaves one
# cycle of slack.
FEED_DUE_ET = (10, 15)
# Full-day NYSE closures, when the feed correctly stays on the prior session.
# A date missing from this list costs one false alert that day, so extend it
# when it runs out.
MARKET_HOLIDAYS = {
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

# Put-flow alerts (added 2026-10-07): "heavy put buying in the Dec and Jan
# expiries" on a tracked name. The feed only carries each contract's running
# volume, so this is "a lot of puts traded", not "someone bought puts". Two
# rules, both limited to expiries FLOW_MIN_DTE+ days out (near-dated strikes on
# the big names put up 1,000 contracts every 15 minutes, far-dated ones do not):
#   block:  one put strike gained FLOW_BLOCK_CONTRACTS+ since the previous run,
#           its day volume exceeds its open interest (new positions, not a
#           close-out), and the day's volume is worth FLOW_BLOCK_NOTIONAL+.
#   expiry: one expiry's puts reach FLOW_EXPIRY_CONTRACTS, at least
#           FLOW_EXPIRY_OI_RATIO of that expiry's put open interest, and
#           FLOW_EXPIRY_PUT_SHARE% of its volume (put-driven, not a straddle
#           or a roll). Calibrated 2026-10-07: 2 expiry hits and about 10 block
#           hits by noon across the 25 names; a flat 1,000-contract rule would
#           have fired 96 times.
# Each key fires once per session; per-contract volumes from the previous run
# live in FLOW_STATE. Hits are always logged (journal) and archived; they are
# posted to Slack only when FLOW_POST=1, to FLOW_SLACK_WEBHOOK_URL or, when that
# is unset, SLACK_WEBHOOK_URL. Posting was switched off 2026-10-07 at the user's
# request, pending threshold tuning.
FLOW_POST = os.environ.get("FLOW_POST", "").strip() == "1"
FLOW_MIN_DTE = 14
FLOW_BLOCK_CONTRACTS = 1000
FLOW_BLOCK_NOTIONAL = 2_000_000
FLOW_EXPIRY_CONTRACTS = 3000
FLOW_EXPIRY_OI_RATIO = 0.5
FLOW_EXPIRY_PUT_SHARE = 70
FLOW_STATE = Path(os.environ.get("TILT_FLOW_STATE") or (
    Path(ARCHIVE_DIR) / "flow_state.json" if ARCHIVE_DIR
    else Path(__file__).resolve().parent / "flow_state.json"))
FLOW_WEBHOOK = os.environ.get("FLOW_SLACK_WEBHOOK_URL", "").strip()

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


def notify_slack(text: str, webhook: str = "") -> None:
    """Best-effort Slack post. Never raises: a broken alert must not fail the run."""
    webhook = webhook or SLACK_WEBHOOK
    if not webhook:
        return
    req = urllib.request.Request(
        webhook,
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


def market_now(now: datetime) -> datetime | None:
    """`now` in New York time, or None when this Python has no tz database
    (stock Windows without the tzdata package). The stale-feed check is skipped
    then; the droplet always has one."""
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return None


def archive_run(today: str, now_iso: str, rows: list[dict]) -> None:
    """Append this run's freshly fetched rows to ARCHIVE_DIR/YYYY-MM-DD.jsonl,
    one JSON line per run. Never raises: the archive must not break the page.

    Unlike tilt.json's rolling history this is never trimmed, and it keeps every
    15-min reading (calls, puts, spot, expiry), not just the day's last one, so
    an intraday reading can be tested against the price later that session.
    Carried-forward stale rows are left out; a run with nothing fresh writes no
    line. Readings stamped before ~9:45am ET are still the prior session's totals
    (see EMPTY above), so a backtest should start each day at the 10:00am ET run.
    """
    if not ARCHIVE_DIR:
        return
    fresh = [r for r in rows if r.get("updated") == now_iso]
    if not fresh:
        return
    try:
        folder = Path(ARCHIVE_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"generated": now_iso, "rows": fresh}, separators=(",", ":"))
        with open(folder / f"{today}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"  archive write failed: {e}", file=sys.stderr)


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
    # so skip it and roll to Monday's instead. Mon-Thu keep 0DTE. Most tickers only
    # list Mon/Wed/Fri expiries, so on Tue/Thu they roll to Wed/Fri naturally below;
    # the ones with an expiry every weekday (GLD, SMH, XLF) score same-day all week.
    friday_skip_same_day = now_local.weekday() == 4

    # Bucket volume by expiration. `session` is the day that volume was traded:
    # a contract's volume and its last_trade_time move together (a contract not
    # traded this session keeps an older date and shows zero volume), so the
    # newest trade date among contracts carrying volume is the session the file
    # is on. The underlying's own last_trade_time is no use here, it ticks in
    # the pre-market while the option volume is still yesterday's.
    by_exp: dict[str, list[int]] = {}
    session = ""
    # Side channel for the put-flow check (see FLOW_* above): every expiry's
    # volume and open interest by side, plus each far-dated put that traded.
    # Stripped from the row before it is written, so none of it is public.
    exp_flow: dict[str, dict] = {}
    far_puts: list[dict] = []
    far_cutoff = (now_local + timedelta(days=FLOW_MIN_DTE)).strftime("%y%m%d")
    for o in data.get("options", []):
        m = OCC.match(o.get("option", ""))
        if not m:
            continue
        exp, cp = m.group(1), m.group(2)
        v = int(o.get("volume") or 0)
        oi = int(o.get("open_interest") or 0)
        bucket = by_exp.setdefault(exp, [0, 0])
        bucket[0 if cp == "C" else 1] += v
        ef = exp_flow.setdefault(f"20{exp[:2]}-{exp[2:4]}-{exp[4:]}",
                                 {"calls": 0, "puts": 0, "call_oi": 0, "put_oi": 0})
        ef["calls" if cp == "C" else "puts"] += v
        ef["call_oi" if cp == "C" else "put_oi"] += oi
        if v:
            day = ISO_DAY.match(str(o.get("last_trade_time") or ""))
            if day and day.group() > session:
                session = day.group()
            if cp == "P" and exp >= far_cutoff:
                far_puts.append({
                    "occ": m.group(0), "exp": f"20{exp[:2]}-{exp[2:4]}-{exp[4:]}",
                    "strike": int(m.group(3)) / 1000, "vol": v, "oi": oi,
                    "last": float(o.get("last_trade_price") or 0),
                    "bid": float(o.get("bid") or 0), "ask": float(o.get("ask") or 0),
                })

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
        "session": session or None,
        "near": near,
        "chain": {"calls": calls_all, "puts": puts_all, "total": total_all,
                  "tilt": round(calls_all / total_all * 100, 1)},
        "_flow": {"exp": exp_flow, "puts": far_puts},
    }


def fmt_expiry(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d:%b} {d.day}"


def fmt_strike(k: float) -> str:
    return f"{k:g}"


def trade_side(p: dict) -> str:
    """Where the last print sat against the quote. One print standing in for the
    whole day, so a hint only."""
    if p["last"] <= 0 or p["ask"] <= 0:
        return ""
    if p["last"] >= p["ask"]:
        return "last print at the ask"
    if p["last"] <= p["bid"]:
        return "last print at the bid"
    return "last print between the quotes"


def flow_run(flows: dict[str, dict], market_day: str, when: str) -> None:
    """Put-flow alerts for this run (see FLOW_* above), one Slack post per run
    listing every new hit. `flows` holds the `_flow` side channel of each row
    fetched fresh and on today's session. Best-effort: never raises."""
    if not flows:
        return
    try:
        state = json.loads(FLOW_STATE.read_text()) if FLOW_STATE.exists() else {}
    except Exception:
        state = {}
    if state.get("date") != market_day:
        state = {"date": market_day, "last": {}, "fired": []}
    fired = set(state["fired"])
    far_from = (datetime.strptime(market_day, "%Y-%m-%d") + timedelta(days=FLOW_MIN_DTE)).strftime("%Y-%m-%d")
    lines = []
    for sym, flow in flows.items():
        last = state["last"].get(sym, {})
        cur = {}
        for p in flow["puts"]:
            cur[p["occ"]] = p["vol"]
            price = p["last"] if p["last"] > 0 else (p["bid"] + p["ask"]) / 2
            notional = p["vol"] * price * 100
            jump = p["vol"] - last.get(p["occ"], 0)
            key = f"{sym}|{p['exp']}|{p['strike']}"
            if (jump >= FLOW_BLOCK_CONTRACTS and p["vol"] > p["oi"]
                    and notional >= FLOW_BLOCK_NOTIONAL and key not in fired):
                fired.add(key)
                side = trade_side(p)
                lines.append(
                    f"*{sym} {fmt_expiry(p['exp'])} {fmt_strike(p['strike'])}P*: +{jump:,} contracts "
                    f"since the last check, {p['vol']:,} on the day vs {p['oi']:,} open interest, "
                    f"last ${price:.2f} (~${notional / 1e6:.1f}M)" + (f", {side}" if side else "") + ".")
        state["last"][sym] = cur
        for exp, e in flow["exp"].items():
            if exp < far_from or not e["put_oi"]:
                continue
            tot = e["calls"] + e["puts"]
            share = e["puts"] / tot * 100 if tot else 0
            key = f"{sym}|{exp}"
            if (e["puts"] >= FLOW_EXPIRY_CONTRACTS and e["puts"] >= FLOW_EXPIRY_OI_RATIO * e["put_oi"]
                    and share >= FLOW_EXPIRY_PUT_SHARE and key not in fired):
                fired.add(key)
                top = sorted((p for p in flow["puts"] if p["exp"] == exp), key=lambda p: -p["vol"])[:3]
                tops = ", ".join(f"{fmt_strike(p['strike'])}P {p['vol']:,}" for p in top)
                lines.append(
                    f"*{sym} {fmt_expiry(exp)} expiry*: {e['puts']:,} puts today, "
                    f"{e['puts'] / e['put_oi']:.1f}x the open interest, {share:.0f}% of the expiry's "
                    f"volume. Top strikes: {tops}.")
    state["fired"] = sorted(fired)
    try:
        FLOW_STATE.parent.mkdir(parents=True, exist_ok=True)
        FLOW_STATE.write_text(json.dumps(state, separators=(",", ":")))
    except Exception as e:
        print(f"  flow state write failed: {e}", file=sys.stderr)
    # Per-expiry daily totals, overwritten each run so the file ends the day with
    # the close. Baseline material for "unusual vs this expiry's normal" later.
    if ARCHIVE_DIR:
        try:
            folder = Path(ARCHIVE_DIR) / "flow"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{market_day}.json").write_text(json.dumps(
                {"generated": when, "expiries": {s: f["exp"] for s, f in flows.items()}},
                separators=(",", ":")))
        except Exception as e:
            print(f"  flow archive write failed: {e}", file=sys.stderr)
    if lines:
        msg = ":large_blue_circle: Put flow (volume, not confirmed buys):\n" + "\n".join(lines)
        if FLOW_POST:
            notify_slack(msg, FLOW_WEBHOOK)
        print(msg if FLOW_POST else msg.replace("Put flow", "Put flow (logged, not posted)", 1))


def main() -> int:
    # Load prior file so history and last-known-good rows carry forward.
    prior_history: dict[str, list] = {}
    prior_rows: dict[str, dict] = {}
    prior_alert: dict = {}
    if OUT.exists():
        try:
            prior = json.loads(OUT.read_text())
            prior_history = prior.get("history", {})
            prior_rows = {r["symbol"]: r for r in prior.get("rows", [])}
            prior_alert = prior.get("feed_alert") or {}
        except Exception:
            pass

    now_dt = datetime.now(timezone.utc)
    today = now_dt.astimezone().strftime("%Y-%m-%d")
    now_iso = now_dt.isoformat(timespec="seconds")
    # The trading day in New York, which is what a row's `session` is compared
    # against. Falls back to the local date when there is no tz database.
    now_et = market_now(now_dt)
    market_day = now_et.strftime("%Y-%m-%d") if now_et else today
    rows, failed, empty, behind, fetched = [], [], [], [], []
    flows: dict[str, dict] = {}
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
        flow = row.pop("_flow", None)
        row["updated"] = now_iso
        rows.append(row)
        fetched.append(row)

        # A row still on an earlier session's volume is shown as-is (it is the
        # latest there is) but must not be written into history under today's
        # date: that is how the 09/23 duplicates and the Labor Day ghost row got
        # in. An unknown session (no trade times in the payload) is let through.
        on_prior_session = bool(row["session"]) and row["session"] < market_day
        if on_prior_session:
            behind.append(sym)
        else:
            if flow:
                flows[sym] = flow
            hist = [h for h in prior_history.get(sym, []) if h["date"] != today]
            hist.append({
                "date": today, "near": row["near"]["tilt"], "chain": row["chain"]["tilt"],
                "near_volume": row["near"]["total"], "chain_volume": row["chain"]["total"],
            })
            prior_history[sym] = hist[-HISTORY_KEEP:]

        # Day-over-day change per view, if we have a previous date.
        prev = [h for h in prior_history.get(sym, []) if h["date"] != today]
        p = prev[-1] if prev else {}
        row["near"]["tilt_prev"] = p.get("near", p.get("tilt"))   # old entries stored "tilt"
        row["chain"]["tilt_prev"] = p.get("chain")
        print(f"  {sym}: near {row['near']['tilt']} / chain {row['chain']['tilt']}"
              + (f" (still {row['session']} volume)" if on_prior_session else ""))

    # Stale feed: past FEED_DUE_ET on a trading day, any row still on a prior
    # session is a problem. Before that time, and on weekends and holidays, it
    # is simply what the feed is supposed to look like.
    feed_due = (now_et is not None and now_et.weekday() < 5
                and market_day not in MARKET_HOLIDAYS
                and (now_et.hour, now_et.minute) >= FEED_DUE_ET)
    stale_feed = behind if feed_due else []
    feed_down = len(stale_feed) > len(SYMBOLS) / 2
    # Can't check anything if no fetched row could be dated (Cboe changed its payload).
    blind = feed_due and bool(fetched) and not any(r["session"] for r in fetched)
    # One Slack post per day per symbol, not one per 15-min run: `feed_alert`
    # rides along in tilt.json so the next run knows what was already reported.
    feed_alert = prior_alert if prior_alert.get("date") == market_day else {}
    feed_alert = {"date": market_day, "symbols": list(feed_alert.get("symbols", [])),
                  "blind": bool(feed_alert.get("blind"))}
    new_stale = [s for s in stale_feed if s not in feed_alert["symbols"]]
    new_blind = blind and not feed_alert["blind"]
    feed_alert["symbols"] += new_stale
    feed_alert["blind"] = feed_alert["blind"] or blind

    out = {
        "generated": now_iso,
        "source": "Cboe delayed quotes (15-min delay; intraday values are partial-day). near = nearest expiration incl. same-day 0DTE (Mon-Thu; Fridays roll to the next expiry), rolling past sub-1,000-contract expiries; chain = all expirations",
        "rows": rows,
        "failed": failed,
        "empty": empty,
        "behind": behind,
        "feed_alert": feed_alert,
        "history": prior_history,
    }
    OUT.write_text(json.dumps(out, indent=1))
    print(f"Wrote {OUT} ({len(rows)} symbols, {len(failed)} failed, {len(empty)} empty, "
          f"{len(behind)} on a prior session)")
    archive_run(today, now_iso, rows)
    flow_run(flows, market_day, now_iso)

    if new_stale:
        link = run_url()
        days = ", ".join(sorted({r["session"] for r in fetched if r["symbol"] in stale_feed}))
        if feed_down:
            msg = (f":rotating_light: Tilt Score: the Cboe feed has not rolled to today's "
                   f"session. {len(stale_feed)} of {len(SYMBOLS)} symbols are still on {days} "
                   f"volume at {now_et:%H:%M} ET, so the page is showing a prior session's "
                   f"numbers as current. If today is a market holiday, add it to "
                   f"MARKET_HOLIDAYS in fetch_tilt.py.")
        else:
            msg = (f":warning: Tilt Score: {', '.join(stale_feed)} still on {days} volume at "
                   f"{now_et:%H:%M} ET ({len(stale_feed)} of {len(SYMBOLS)} symbols). "
                   f"The rest are current.")
        notify_slack(msg + (f"\n{link}" if link else ""))
        print(msg, file=sys.stderr)
    if new_blind:
        msg = (":warning: Tilt Score: the stale-feed check is off. The Cboe payload no longer "
               "carries option trade times, so fetch_tilt.py cannot tell which session the "
               "volume belongs to.")
        notify_slack(msg)
        print(msg, file=sys.stderr)

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

    # A feed stuck on a prior session keeps `updated` advancing, so it would pass
    # the freshness test above. Fail the run on it too, so __main__ pings /fail.
    return 0 if healthy and not feed_down else 1


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
