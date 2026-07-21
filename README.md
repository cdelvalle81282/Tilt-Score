# Tilt Score (Project Next)

Daily 0DTE / nearest-expiration call/put tilt for 17 tickers. Data from Cboe
delayed quotes. A scheduled GitHub Action refreshes `tilt.json` each weekday
after the close; `index.html` renders it.

Tilt scores the **nearest** expiration for each name, keeping the same-day
(0DTE) expiry (this is a 0DTE/1DTE service, so the near expiry is the point).
Any expiry trading under `VOLUME_FLOOR` (1,000 contracts) is skipped to the
next real one, so a dead near-dated expiry (e.g. a 40-contract Wednesday)
never prints a noisy score.

## Files

- `index.html`: the page. Self-contained (fonts from Google, banner embedded).
  Because it is named `index.html`, GitHub Pages serves it at the repo root URL.
- `tilt.json`: the data, refreshed daily by the Action. Also carries the
  per-symbol history that powers the Δ1d column, so don't delete it.
- `fetch_tilt.py`: the fetcher. Stdlib only, no dependencies.
- `.github/workflows/update-tilt.yml`: the schedule (5:45pm ET weekdays, plus a
  manual "Run workflow" button in the Actions tab) and the failure alerting.

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

The workflow and the fetcher alert on every failure mode, loud or silent. All
of it is opt-in through two repo secrets; leave a secret unset and that path is
a quiet no-op.

- **Hard failures** (script crash, all symbols down, push denied, 10-minute
  timeout): the job fails and the `Alert on failure` step posts to Slack and
  signals healthchecks.io.
- **Partial failures** (some symbols in `failed[]` but the page still updates):
  the job stays green, so `fetch_tilt.py` posts its own Slack heads-up naming
  the symbols. Without this these are invisible outside the page footer.
- **Silent stop** (cron auto-disabled after ~60 days idle, GitHub outage,
  someone disables the workflow): a workflow can't report its own
  non-execution, so a successful run pings a healthchecks.io check; if a
  scheduled ping goes missing, that service alerts you.

Repo secrets (Settings, Secrets and variables, Actions):

- `SLACK_WEBHOOK_URL`: a Slack incoming-webhook URL for the alert channel.
- `HEALTHCHECK_URL`: the healthchecks.io ping URL (e.g. `https://hc-ping.com/<uuid>`,
  no trailing slash). Configure that check's schedule to `45 21 * * 1-5` (UTC)
  with a grace of about 1 hour, and point its notifications wherever you want.

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
