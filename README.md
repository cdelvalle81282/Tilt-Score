# Tilt Score — Project Next

Daily front-expiration call/put tilt for 17 tickers. Data from Cboe delayed
quotes. A scheduled GitHub Action refreshes `tilt.json` each weekday after
the close; `tilt.html` renders it.

## Files

- `tilt.html` — the page. Self-contained (fonts from Google, banner embedded).
- `tilt.json` — the data, refreshed daily by the Action. Also carries the
  per-symbol history that powers the Δ1d column, so don't delete it.
- `fetch_tilt.py` — the fetcher. Stdlib only, no dependencies.
- `.github/workflows/update-tilt.yml` — the schedule (5:45pm ET weekdays,
  plus a manual "Run workflow" button in the Actions tab).

## Setup (one time)

1. Create a **public** repo (free GitHub Pages requires public unless you
   have a paid plan) and upload all four files, keeping the workflow at
   exactly `.github/workflows/update-tilt.yml`.
2. Settings → Pages → Source: **Deploy from a branch** → `main`, `/ (root)`.
3. Settings → Actions → General → Workflow permissions: confirm
   **Read and write permissions** is selected (the workflow needs to commit
   `tilt.json` back).
4. Actions tab → "Update tilt scores" → **Run workflow** once to test.
   A green run should produce a new commit touching `tilt.json`.
5. Page is live at `https://YOURUSER.github.io/YOURREPO/tilt.html`.

## Embedding in wisepub

Two options:

- **Iframe** the Pages URL. Zero maintenance — the embedded page keeps
  updating on its own.
- **Paste the HTML** into wisepub. Then edit one line near the bottom of
  `tilt.html`: set `DATA_URL` to the absolute JSON address,
  `https://YOURUSER.github.io/YOURREPO/tilt.json`. GitHub Pages serves with
  open CORS, so the cross-origin fetch works. The copy in wisepub then pulls
  fresh data on every load without being re-pasted.

## Known behaviors

- Market holidays: the Action still runs; if Cboe reports zero volume the
  symbol records no history entry for that day. Harmless.
- GitHub disables cron schedules in repos with no activity for ~60 days;
  the daily bot commit normally keeps it alive, but if GitHub emails a
  "scheduled workflow disabled" warning, one click re-enables it.
- Δ1d column hides itself until `tilt.json` contains at least two days of
  history.
