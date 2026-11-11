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

Google sign-in requires a Firebase Admin service-account credential mounted at
`/etc/secrets/firebase-adminsdk.json` on Render or saved as
`backend/firebase-adminsdk.json` locally. Keep this private JSON out of Git.
If the credential is missing, private dashboard reads and writes return
`503` with `Cache-Control: no-store`; local development has no authentication
bypass. Health checks, API documentation, OAuth discovery and explicitly public
media remain available. Existing agent codes, MCP OAuth tokens and website API
keys keep their own credential and current-owner permission checks.

The reviewed employee roster, Slack mappings, avatar fallback URLs, role
exceptions and historical roster migrations are private configuration. Mount
`/etc/secrets/sentient-roster.json` on the API and ingestion worker, or set
`SENTIENT_ROSTER_FILE` to its private file path. Local development reads
`backend/sentient-roster.json` when no explicit path is set. Use mode `0600`
for local files; these files are ignored by Git. The JSON uses `version: 1`;
Cortex validates the schema, role overrides and boolean capability flags
before use. An invalid private configuration grants no additional capability.
`backend/tests/fixtures/roster.example.json` shows the schema with fictitious
identities. Keep the production migration markers unchanged so existing
databases retain their completed migrations and subsequent Settings edits.
Without this file, Cortex uses existing database roles, grants no roster
exceptions, seeds no roster identities and has no private Slack recipient mappings.
Configure both services before releasing a change that moves roster metadata.

`GET /api/health` remains public. Its optional `verify=true` database readiness
checks require an authenticated Admin or Dev and use private, noncached responses.

Apify HTTP 408/429/5xx and transport failures retain the ingestion slot and its
paid-run journal, retrying after 2, 4, 8, 16 and then 30 minutes. Unconfirmed
starts and unfinished runs resume every 2 minutes. These retries do not consume
the five-attempt cap for deterministic processing failures. Missing or rejected
credentials (401/403) need a valid `APIFY_TOKEN` in both Render services:
`cortex-ingestion-worker` and `cortex-api`. A redeploy preserves the retry deadline;
allow up to 30 minutes before the next attempt. The discovery monitor reports
one provider incident and one recovery once successful discovery is current.
The DEV ingestion-status endpoint exposes `retry_reason`, `retry_started_at`
and `transient_attempts` without returning saved payloads or credentials.

## What it actually serves today

- `/api/dashboard/*` — posts, accounts, queue (tasks/assign/reorder), saved
  lists, media listing/zip-download, avatar/cover proxying.
- `/api/admin/*` — accounts CRUD + backfill, users/roles, usage heatmap,
  Apify run history/enrich/scrape-missing controls, OCR status, Slack
  test/custom-alert, disk status.
- `/api/tracker/*` — daily follower snapshots, calendar-day growth deltas,
  per-account/batch refresh. Refresh submissions return a durable job; poll
  `/api/tracker/refresh-jobs/{job_id}` until it finishes and inspect per-account
  failures before reporting success. Concurrent requests share active work;
  a later refresh starts a new Instagram reading without a time-based cooldown.
  Individual refreshes have their own worker lane, so a full refresh cannot
  hold up a selected account. Profiles use Apify's dedicated profile scraper,
  with the general scraper as a fallback for missing readings. Full refreshes
  batch profiles and retry only missing readings. Only matching
  profiles with exact integer follower counts replace stored data. Snapshot
  dates use Costa Rica time, and recovered older observations cannot overwrite
  a newer reading for the same day.
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

### Dashboard companion

`GET /api/dashboard/assistant/status` and `POST /api/dashboard/assistant/chat`
require the signed-in Firebase browser session. Chat uses the existing
`OPENAI_API_KEY`; `OPENAI_ASSISTANT_MODEL` optionally overrides the existing
caption model (`OPENAI_CAPTION_MODEL`, default `gpt-5-mini`). No new secret is
needed for an installation already generating captions.

The assistant searches stored dashboard data first for every factual request,
not only shortcuts; it does not substitute web results or unsupported model
knowledge for missing records. Answers stay concise and use internal post links.

Assistant-generated Research searches use at most three main words, selecting
a meaningful phrase instead of copying the request. Account, date and engagement
criteria stay in their filters; longer exact phrases are verified against the
retrieved captions. Oversized tool queries are rejected for reformulation before
execution. Direct API searches and the user's original message are unchanged.

Chat accepts modes `auto`, `filter`, `research`, and `next`, or a leading
case-insensitive slash shortcut (the prefix takes precedence). Automatic typed
and spoken queries use one combined language/intent decision, defaulting to
intelligent research of stored dashboard data. They apply page filters only for
a browsable set fully represented by existing controls. Factual questions,
exact-N lists/rankings, semantic matches, and unsupported conditions stay in
research; only resolved filter requests can execute a page-filter action.
The structured routing decision separately extracts result shape, requested
post count, and whether page controls satisfy the whole request. The server
rejects automatic filtering unless all three agree on an unbounded, representable
post set; metric thresholds do not count as requested result counts.
Explicit shortcuts and selected modes take precedence. `/filter` applies
existing Research page controls; `/research` answers in chat without changing
filters. `/next` recommends 1–10 stored competitor posts to recreate for a
Sentient account (one by default). Its `option_count` stays with missing
reel/image/carousel format and destination during clarification. It checks each
candidate against the destination's history and reviews up to four batches to
fill the requested count. If a later batch fails or the review budget runs out,
it can select from the already verified subset and briefly report the shortfall;
it never bypasses selection or adds unreviewed posts. A bounded `pending_next` response can be returned as
`next_context` on the clarification reply; a fresh slash command resets it.
Destination accounts and competitor groups come from the live registry. The
shortlist uses exact-format, recent stored rankings plus captions/OCR and target
account examples; links and selection keys are validated by the server. It does
not claim that a recommendation has never been recreated or queued.

All authenticated, allowlisted Firebase users receive the same assistant-only
read access to reviewed dashboard product routes, independent of their UI role.
Each internal read revalidates the original session and carries a private,
route-bound in-process capability that cannot be supplied by an HTTP header.
Identity reads retain the real caller. Normal browser/MCP roles do not change.
The read capability excludes mutations, credentials, binary exports, operational logs,
paid refreshes and external-fetch GETs. Queue retention, stack bootstrapping and Hooks
synchronization/categorization are suppressed in this read context. Sources and
incomplete coverage are retained; failed reads produce a short localized notice.
Explicit Queue action instructions enable a separate reviewed mutation catalogue.
Each exact proposed operation is checked against the current user's instruction,
then executes through ordinary Firebase authentication and that user's real Queue
permissions, including approval rules. Extra notifications require explicit intent.
The time-block action supports `reschedule_conflicts` and `move_remaining_after_block`
for coordinators: it reserves personal time and moves the selected day's unstarted
work atomically, preserving active/finished work, other users and existing future
placements. The response includes moved request IDs and a count.

Mutation turns require a stable browser `request_id` UUID. Durable per-user receipts
prevent a lost response or another worker from repeating the same turn. Ambiguous
writes require state reconciliation; no write is retried automatically. Confirmed
changes emit `queue_updated`, refreshing Queue without reloading the chat.
OpenAI Responses calls use `store: false`. Cortex stores only the request fingerprint
and public final response for mutation-turn replay, rather than a conversation log.
Each API process limits a user to one active answer, 10 requests per minute
and 60 per hour, with a 120-second answer deadline.

### Release

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

Admin and Dev agents can read individual HOT calibration data using
`GET /api/admin/engagement/checkpoints` or the hosted MCP tool
`get_admin_engagement_checkpoints`. Read-only connections are supported.
Filter by `account` and `days` (1–90, default 30), and paginate with
`limit` (1–100) and `offset`. The response includes account thresholds and
30-day suggestions, individual first-HOT-check likes, stored later checkpoints,
publication metadata and measurement coverage. New checks preserve their
receipt/evaluation times and the threshold actually applied. Legacy values
remain available with unknown historical metadata represented as `null`.
Reading this endpoint never starts an Instagram scrape or sends notifications.

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
Custom hostnames require `SENTIENT_MCP_ALLOWED_HOSTS`.

## ChatGPT MCP OAuth

Hosted MCP also supports OAuth authorization code with PKCE S256 and public
dynamic client registration. Existing agent codes keep their current format,
permissions, expiry and management API. OAuth discovery is public at
`/.well-known/oauth-protected-resource/mcp` (also the root variant) and
`/.well-known/oauth-authorization-server`. Registration, authorization, token
exchange and revocation are served by `/oauth/register`, `/oauth/authorize`,
`/oauth/token` and `/oauth/revoke`.

Only registered HTTPS ChatGPT callbacks are accepted. Authorization redirects
to SentientDash `/oauth.html`, where the owner signs in with Firebase and
explicitly approves or declines the requested permissions. Browser APIs are
`GET/POST /api/dashboard/me/oauth/authorization`,
`GET /api/dashboard/me/oauth-connections` and
`DELETE /api/dashboard/me/oauth-connections/{grant_id}`. Delegated credentials
cannot use these APIs, and consent cannot run under a role preview.

The `sentient:read` scope permits read tools; `sentient:write` adds actions
within the owner's current roles, with the existing `confirm=true` gate.
OAuth tool metadata and scope-upgrade challenges let clients request writes
explicitly. Access tokens last 15 minutes; connections last 90 days. Refresh
tokens rotate, and reuse revokes the entire connection. All secret values
are stored as SHA-256 hashes, and OAuth responses use no-store caching.

OAuth access-token validation failures caused by storage outages or unexpected
server errors return `503` with `Retry-After: 2`, without an authentication
challenge. Invalid, expired or revoked tokens still return `401 invalid_token`.
Logs for unexpected validation failures use a fixed message without exception
details or tracebacks. Clients must serialize refreshes per connection:
simultaneous reuse of one refresh token revokes the entire connection, including
the replacement tokens, under the existing replay-detection policy.

Tokens are bound to the canonical `/mcp` resource and are rejected on direct
REST requests. Internal tool dispatch revalidates the token and owner's
permissions without forwarding it as a REST bearer credential. Revocation
stops subsequent calls and leaves independently issued agent codes intact.

`SENTIENT_OAUTH_ISSUER` defaults to `https://cortex-api-db2e.onrender.com`.
An override must be an HTTPS origin and must match the public deployment;
custom MCP hosts must also be configured above. The frontend consent URL is
`https://sentientdash.app/oauth.html`. No additional shared OAuth secret is
required. Schema creation is additive through the existing startup migration.

OAuth protocol, replay, permission isolation and legacy compatibility tests
are in `backend/tests/test_mcp_oauth.py` and
`backend/tests/test_mcp_oauth_integration.py`. Deterministic SQLite rotation
concurrency and rollback coverage is in `backend/tests/test_mcp_oauth_concurrency.py`.

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
- `GET /api/v1/accounts/{handle}/posts/{shortcode}`
- `GET /api/v1/accounts/{handle}/posts/{shortcode}/cover`
- `GET /api/v1/posts/{shortcode}`
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

A single post by Instagram shortcode returns the same projection as a list
item plus `cover_url`, and returns 404 for anything the list would exclude
(hidden, deleted, future, private-account or out-of-scope posts). The cover
route redirects to the post's stored cover (R2) and returns 404 when no cover
has been stored; it never fetches from Instagram.
`GET /api/v1/posts/{shortcode}` finds the same post without knowing its
account, searching only the key's accounts, and adds `account` to the result.
Single-post reads query only that shortcode's rows.

Posts and media kit standout posts expose `is_promo`: the manual Research
flag or a case-insensitive `#aitoolsentient` caption hashtag, using Research's
word boundary (so `#aitoolsentientlabs` does not match). Posts accept the
optional `is_promo=true|false` filter before pagination; omitting it returns
all public posts. Media kit summaries keep their existing mixed population.

Posts and media kit standout posts also expose `is_collab: true|false|null`
and `collaborators`, a deduplicated list of lowercase Instagram handles that
excludes the requested account. Classification uses only stored explicit
coauthor metadata, never mentions, tagged users or Promo status. A valid
empty list confirms false; a self-only list confirms false only when the
known primary owner is also the requested account. Missing or unusable
metadata without sufficient valid evidence remains null. Valid other handles
still confirm a collaboration when some entries are malformed; the returned
list contains available participants rather than a guaranteed full inventory.
When explicit coauthors include the requested account,
a known different primary owner joins the collaborator list. Newer usable
explicit metadata supersedes older evidence; updates without usable coauthor
metadata preserve existing evidence. No collaboration query filter is added.

The [Spanish integration guide](https://sentientdash.app/api-guide.html?lang=es)
and [English integration guide](https://sentientdash.app/api-guide.en.html?lang=en)
include a downloadable Node website proxy example, private environment
variables, five-minute website caching, field mapping, and error handling.
Regression coverage lives in `backend/tests/test_external_api.py`.
