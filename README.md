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
  Its editorial layout pairs an audience headline with a public profile card,
  concise performance summaries, and image-led content examples. Prices and
  commercial packages are excluded.
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

## Website data API

Admin/Dev users issue account-scoped, read-only website keys at Sentient
Dash `/api.html`. Management uses a Firebase browser session:
`GET/POST /api/dashboard/me/api-keys` and
`DELETE /api/dashboard/me/api-keys/{key_id}`. Secrets are returned once,
stored only as SHA-256 hashes, expire after 30/90/365 days, and can be revoked.
Each owner can have 20 active keys and select up to 100 active accounts per key.

Website requests require `Authorization: Bearer sad_api_…` and can read only:

- `GET /api/v1/accounts`
- `GET /api/v1/accounts/{handle}`
- `GET /api/v1/accounts/{handle}/media-kit`
- `GET /api/v1/accounts/{handle}/posts`
- `GET /api/v1/accounts/{handle}/followers/history`

The owner's current allowlist/Admin/Dev access, selected account scope, and
account activity are checked on every request. Keys cannot access internal
dashboard routes, create credentials, perform writes, or connect to MCP.
The persistent atomic rate limit is 60 calls per minute per key; excess calls
return 429 with `Retry-After`. Responses use private no-store caching.

Responses declare `schema_version: "1.0"`. Media kit/profile responses
distinguish `generated_at` from `data_updated_at.profile` and `.engagement`.
Posts/history accept `limit` (1–100), `offset` (non-negative), and optional
inclusive Costa Rica calendar dates `from`/`to`. Posts are newest first;
history is oldest first with the final valid follower sample for each day.
Pagination includes total, has_more, and next_offset. Missing readings remain
null; insufficient recent coverage omits last_30_days. Public projections
exclude hidden/deleted/private posts, internal labels, contacts and model data.
Reads never start a scrape or refresh.

The [Spanish integration guide](https://sentientdash.app/api-guide.html)
includes a downloadable Node website proxy example, private environment
variables, five-minute website caching, field mapping, and error handling.
Regression coverage lives in `backend/tests/test_external_api.py`.
