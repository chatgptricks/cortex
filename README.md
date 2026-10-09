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

## Runtime storage

Sentient Dash uses Postgres for production data and Cloudflare R2 for every
uploaded cover, avatar, alert image, and Queue attachment. Runtime requests do
not write persistent media to the Render filesystem. `SENTIENT_DATA_DIR` only
supports the local SQLite fallback used during development.

## Local development

```bash
scripts/dev.sh
```

- Frontend (if running the old dev script alongside): http://127.0.0.1:5173
- Backend: http://127.0.0.1:8000/api/health

Requires a `.env` — copy `.env.example` and fill in what you need locally
(Slack webhook, Apify token, Firebase service-account path, R2 credentials,
etc.). Without `SENTIENT_DATA_DIR` set it defaults to `./data` relative to the
repo root.

## What it actually serves today

- `/api/dashboard/*` — posts, accounts, queue (tasks/assign/reorder), saved
  lists, media listing/zip-download, avatar/cover proxying.
- `/api/admin/*` — accounts CRUD + backfill, users/roles, usage heatmap,
  Apify run history/enrich/scrape-missing controls, OCR status, Slack
  test/custom-alert, disk status.
- `/api/tracker/*` — daily follower snapshots, calendar-day growth deltas,
  per-account/batch refresh.
- `/api/insights/*` — aggregate stats for `insights.html`.
- `GET /api/admin/accounts/{handle}/media-kit` and `media-kit.pdf` — fresh
  account media-kit data and a downloadable PDF for Admin/Dev Settings.
  The two-page PDF is a client-shareable sales overview with public profile,
  audience size, selected public performance highlights, and standout posts.
  A strict public-data projection excludes internal labels, model signals,
  tracking counters, contact details, and granular appendices before rendering.
  Values use at most two decimals and the generating user's selected theme
  and accent. The authenticated JSON retains the complete internal data
  and sample sizes. Both read existing data on every call, use private no-store
  responses, and never initiate a scrape. Historical totals describe the
  saved sample, and missing/hidden measurements remain unavailable.
  Recent highlights require a current full-post source and sufficient
  30-day history without contradictory profile-count changes. Narrow Reels
  sources or incomplete periods omit the recent section instead of presenting
  a partial sample as account activity. Recent metric totals also require
  every applicable post to have a public reading; partial sums are omitted.
- `/api/auth/custom-token` — mints a short-lived Firebase custom token so
  the several Sentient Dash subdomains/pages can share one signed-in
  session.

Auth is Firebase ID tokens plus an email allowlist enforced by the
`_require_firebase_user` middleware. Some admin mutation routes also retain
the server-side `TRICKS_DASH_REFRESH_PASSWORD` check.

## Deploying

Render auto-deploys on push to `main` (see `render.yaml`). Confirm a deploy actually landed via
`GET /api/health`, which reports the live `commit` hash. **Never push to
`main` while an account import/backfill is running** — a redeploy restarts
the process and silently kills it. See `FOR_CODEX.md` for the full deploy
workflow and why the mounted working copy's local git state can look
stale/dirty without anything being wrong.

## Remote GPU / OCR workers

The active Modal worker is a **standalone, GPU-free cover-image OCR worker**
(`workers/`, client in `backend/app/sentient_ocr.py`, configured via
`SENTIENT_OCR_URL` / `SENTIENT_OCR_TOKEN`) — it reads text baked into
Instagram cover images for search indexing and hook display.

## Agent connection credentials

Authenticated users can create and revoke their own agent connection codes
at the frontend's `/agents.html` page. Cortex stores hashes, expiry and last
use metadata; codes inherit the owner's current roles on each request.
Agent codes cannot manage credentials or mint Firebase sessions. Read-only
codes reject mutation methods server-side. Management API:
`GET/POST /api/dashboard/me/agent-connections`,
`DELETE /api/dashboard/me/agent-connections/{connection_id}`.
These endpoints require the owner's signed-in browser identity.

Hosted MCP is served at `/mcp` on the same Cortex HTTPS origin. Clients
supply `Authorization: Bearer <agent-connection-code>` on each request.
The stateless MCP server discovers tools from the running API schema and
routes calls internally through the existing authentication/authorization
middleware. Full-access codes expose action tools with `confirm=true`;
read-only codes hide those tools and reject direct mutation calls.
Browser Origin requests and unexpected Host headers are rejected.
Custom hostnames require `SENTIENT_MCP_ALLOWED_HOSTS`. OAuth is not advertised.
