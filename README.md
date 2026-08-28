# Cortex (Sentient Dash backend)

FastAPI backend for **Sentient Dash** (`sentientdash.app`), Sentient
Agency's Instagram analytics + content-queue dashboard. Deployed on Render
at `cortex-api-db2e.onrender.com`. Almost everything lives in one file,
`backend/app/main.py` (~60 routes).

For the full picture (feature set, deploy workflow, frontend relationship,
gotchas, backlog) see **`FOR_CODEX.md`** at the repo root. This README only
covers local setup. Also read **`APIFY_OPERATIONS_LEARNINGS.md`** before
touching Apify, the scheduler, or the database — it's a list of hard-won,
real-cost rules, not a nice-to-have.

## This repo used to be a different app

This folder/repo is still named after **Predict**, an earlier, unrelated
product: a TRIBE v2 (fMRI-style) cover-image analyzer with A/B testing and a
likes-prediction model. That entire feature set has been deleted (source
files removed; only stale `__pycache__` artifacts remain) and the repo was
repurposed as the Sentient Dash backend. A few legacy names survive on
purpose because renaming them would be a real migration for no functional
benefit — notably the database file `predict.sqlite3` and the
`PREDICT_DATA_DIR` / `PREDICT_ALLOWED_ORIGINS` env vars. If you see old
references to TRIBE, calibration models, or Hugging Face weights anywhere,
they're describing the dead product.

## Local development

```bash
scripts/dev.sh
```

- Frontend (if running the old dev script alongside): http://127.0.0.1:5173
- Backend: http://127.0.0.1:8000/api/health

Requires a `.env` — copy `.env.example` and fill in what you need locally
(Slack webhook, Apify token, Firebase service-account path, etc.). Without
`PREDICT_DATA_DIR` set it defaults to `./data` relative to the repo root.

## What it actually serves today

- `/api/dashboard/*` — posts, accounts, queue (tasks/assign/reorder), saved
  lists, media listing/zip-download, avatar/cover proxying.
- `/api/admin/*` — accounts CRUD + backfill, users/roles, usage heatmap,
  Apify run history/enrich/scrape-missing controls, OCR status, Slack
  test/custom-alert, disk status.
- `/api/tracker/*` — daily follower snapshots, calendar-day growth deltas,
  per-account/batch refresh.
- `/api/insights/*` — aggregate stats for `insights.html`.
- `/api/auth/custom-token` — mints a short-lived Firebase custom token so
  the several Sentient Dash subdomains/pages can share one signed-in
  session.

Auth is Firebase ID tokens + an email allowlist
(`_require_firebase_user` middleware), plus a legacy secondary password
(`TRICKS_DASH_REFRESH_PASSWORD`) some admin routes still check.

## Deploying

Render auto-deploys on push to `main` (see `render.yaml` — standard plan,
2GB disk at `/var/data`). Confirm a deploy actually landed via
`GET /api/health`, which reports the live `commit` hash. **Never push to
`main` while an account import/backfill is running** — a redeploy restarts
the process and silently kills it. See `FOR_CODEX.md` for the full deploy
workflow and why the mounted working copy's local git state can look
stale/dirty without anything being wrong.

## Remote GPU / OCR workers

The old TRIBE v2 remote-GPU-worker setup (`REMOTE_TRIBE_URL`,
`scripts/deploy_modal_worker.sh`) is dead along with the rest of Predict.
The one Modal worker still in use is a **standalone, GPU-free cover-image
OCR worker** (`workers/`, client in `backend/app/sentient_ocr.py`,
configured via `SENTIENT_OCR_URL` / `SENTIENT_OCR_TOKEN`) — it reads text
baked into Instagram cover images for search indexing and hook display. It
shares no code or infrastructure with the deleted TRIBE pipeline.
