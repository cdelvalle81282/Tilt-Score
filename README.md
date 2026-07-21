# Tilt Score (Project Next)

0DTE / nearest-expiration call/put tilt for 17 tickers, refreshed **every 15
minutes during market hours**. Data from Cboe delayed quotes.

The page shows two views, toggled at the top:

- **Whole chain** (default): calls / total across all expirations — a standard
  put/call read.
- **Near-term (0DTE/1DTE)**: the nearest expiration, keeping the same-day 0DTE,
  skipping any expiry under `VOLUME_FLOOR` (1,000 contracts) so a dead near-dated
  expiry (e.g. a 40-contract Wednesday) never prints a noisy score.

The **Spread** column (`near − chain`) shows how far the near-term skews from
the whole chain — e.g. XLF's near-term can be call-heavy while its whole chain
is put-heavy.

**How it runs (current):** a job on the ops droplet runs `fetch_tilt.py` every
15 minutes during market hours and serves the result at
`https://tiltnxt.duckdns.org/tilt.json` (open CORS). `index.html` (on GitHub
Pages, or pasted into wisepub) fetches that URL via its `DATA_URL` constant. The
droplet job details live in the workspace ops notes, not this repo. The GitHub
Action below is retired to a manual backup (`workflow_dispatch` only).

## Files

- `index.html`: the page. Self-contained (fonts from Google, banner embedded).
  Because it is named `index.html`, GitHub Pages serves it at the repo root URL.
- `tilt.json`: a seed/backup copy of the data (the live copy is served from the
  droplet). Carries the per-symbol history that powers the Δ1d column.
- `fetch_tilt.py`: the fetcher. Stdlib only, no dependencies. `TILT_JSON`,
  `SLACK_WEBHOOK_URL`, `HEALTHCHECK_URL` env vars configure output path + alerts.
- `.github/workflows/update-tilt.yml`: retired backup workflow (`workflow_dispatch`
  only) that commits `tilt.json` into the repo if run manually.

## Setup (one time)

1. Create a **public** repo (free GitHub Pages requires public unless you have a
   paid plan) and upload the files, keeping the workflow at exactly
   `.github/workflows/update-tilt.yml`.
2. Settings, Pages, Source: **Deploy from a branch**, `main`, `/ (root)`.
3. Settings, Actions, General, Workflow permissions: confirm **Read and write
   permissions** (the workflow commits `tilt.json` back). Without this the run
   fetches fine but the push is denied.
4. Actions tab, "Update tilt scores", **Run workflow** once to test. A green run
   should produce a new commit touching `tilt.json`.
5. Page is live at `https://YOURUSER.github.io/YOURREPO/`.

## Monitoring and alerts

`fetch_tilt.py` self-reports on every failure mode, loud or silent, driven by
two env vars (`SLACK_WEBHOOK_URL`, `HEALTHCHECK_URL`); each path is a quiet
no-op when its var is unset. On the droplet these live in the job's `.env`.

- **Hard failures** (crash, all symbols down): the run posts to Slack and pings
  the healthchecks.io `/fail` endpoint.
- **Partial failures** (some symbols in `failed[]` but the page still updates):
  the run posts its own Slack heads-up naming the symbols. Without this these
  are invisible outside the page footer.
- **Silent stop** (the scheduler/server dies, so no run at all): a successful
  run pings a healthchecks.io check; if a scheduled ping goes missing, that
  service alerts you. Set the check to cron `*/15 13-21 * * 1-5` (UTC) with a
  ~20-minute grace so it catches a missed 15-minute update.

The retired GitHub Action (manual backup) reads the same two values from repo
secrets instead of `.env`.

## Embedding in wisepub

Two options:

- **Iframe** the Pages URL. Zero maintenance: the embedded page keeps updating
  on its own.
- **Paste the HTML** into wisepub. Then edit one line near the bottom of
  `index.html`: set `DATA_URL` to the absolute JSON address,
  `https://YOURUSER.github.io/YOURREPO/tilt.json`. GitHub Pages serves with open
  CORS, so the cross-origin fetch works. The copy in wisepub then pulls fresh
  data on every load without being re-pasted.

## Known behaviors

- Market holidays: the Action still runs; if Cboe reports zero volume the symbol
  records no history entry for that day. Harmless.
- Δ1d column hides itself until `tilt.json` contains at least two days of
  history.
