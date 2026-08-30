# Handover for Codex — Sentient Dash

Originally written by Claude on 2026-08-27; last updated by Codex on 2026-08-30. This file is duplicated identically at the
root of both repos below. Esteban is planning to merge the two repos into
one — read the "Planned repo merge" section near the bottom before you start
that.

## Latest change: standalone Settings command center (2026-08-30)

- Settings is now its own Vite entry and website at `https://sentientdash.app/settings.html`, not a `?view=admin` Dashboard replacement.
- Access is limited to Admin or Dev. The backend `/api/admin/*` boundary accepts either role; old `?view=admin&settingsTab=...` links redirect to the new page.
- The shared gear dropdown on Dashboard, Queue, Tracker, and Insights shows a literal `Settings` link only for Admin/Dev.
- Settings has global links to Dashboard, Tracker, Insights, and Queue, plus the same accent, theme, language, identity, and sign-out gear.
- Tabs are `Overview`, `Accounts`, `Users`, `Usage`, `Notifications`, `System`, and `Reports`. Usage is no longer embedded in Users; Slack status/test/manual alerts are no longer embedded in System.
- Add Account moved out of the Dashboard filters and into Settings → Accounts. Queue's separate Admin/Reports button was removed; Reports has one home in Settings.
- Users centralizes editable display name, operating role, Admin access, Slack ID, and managed Sentient accounts. `dashboard_users.display_name` is the live source; Queue receives it in its scheduler roster and falls back safely for older data.
- Slack profile photos are fetched server-side and served through the same-origin `/api/dashboard/user-avatar/{slack_id}` asset route, so Queue and Settings do not depend on fragile Slack CDN URLs.
- Frontend verification: production build, Queue planner tests, Queue smoke, and Settings smoke. Backend verification: 16 pytest tests.

## What this is

**Sentient Dash** (`sentientdash.app`) is Sentient Agency's internal
Instagram analytics + content-queue tool. It's two repos today:

- **`chatgptricks/tricks-dash`** — the frontend. React 19 + Vite, no
  component-per-file convention: almost the whole dashboard UI is
  `src/App.jsx` (~5,100 lines) and `src/styles.css` (~7,400 lines). Deployed
  as a static site to GitHub Pages, served at `sentientdash.app`.
- **`chatgptricks/cortex`** — the backend. FastAPI, essentially one big
  `backend/app/main.py` (~114k chars, ~60 routes). Deployed on Render at
  `cortex-api-db2e.onrender.com`. Local path on this Mac:
  `/Users/tbnalfaro/Desktop/Codex Projects/10 Predict` (yes, the folder is
  still named "10 Predict" — see "Predict is archived" below).

Local paths as mounted for an agent working on this Mac:
- tricks-dash: `/Users/tbnalfaro/Desktop/Codex Projects/09 Tricks Dash/Tricks Dash`
- cortex: `/Users/tbnalfaro/Desktop/Codex Projects/10 Predict`

**"Predict is archived."** `cortex` used to be a completely different app —
"Cortex by Sentient", a TRIBE v2 fMRI-model cover-image analyzer with A/B
testing and a likes-prediction model (`prediction_v2.py`, `calibration.py`,
`tribe_adapter.py`, etc.). That entire feature set was deliberately deleted
(only `__pycache__` artifacts remain — the `.py` source files are gone). The
repo was repurposed as the Sentient Dash backend. `config.py` and a few other
files still have comments explaining this; the on-disk DB file is still
named `predict.sqlite3` and the `PREDICT_DATA_DIR` / `PREDICT_ALLOWED_ORIGINS`
env var names survive from that era — renaming them would be a real
migration for zero benefit, so it was left alone on purpose. If you find a
doc or comment that talks about TRIBE v2, brain activation, calibration
models, or A/B cover testing, it's describing the dead product, not this one.

## Live surfaces

- `https://sentientdash.app/` — main dashboard (`index.html` / `App.jsx`),
  multi-page Vite build (`vite.config.js` has three entries: `dashboard` →
  `index.html`, `queue` → `queue.html`, `settings` → `settings.html`).
- `https://sentientdash.app/settings.html` — Admin/Dev Settings command center.
- `https://sentientdash.app/queue.html` — Queue board (drag-and-drop content
  calendar, per-user assignment, deep-dive sidebar). Shares
  `src/postDetail.jsx` with the main dashboard's right rail so both surfaces
  render/act on a post identically.
- `https://sentientdash.app/tracker.html` — Social-Blade-style follower
  tracker. Standalone static page (own HTML/CSS/JS under `public/`, not part
  of the Vite React app).
- `https://sentientdash.app/insights.html` — analytics/insights page (word
  cloud, aggregate stats). Also standalone under `public/`.
- Several subdomains (`hot.`, `archive.`, etc.) point at the same deployed
  site and just change the favicon/title client-side based on hostname.
- Backend: `https://cortex-api-db2e.onrender.com` — `GET /api/health`
  reports the live commit hash (`RENDER_GIT_COMMIT`) so you can confirm a
  deploy actually landed.

## Feature set (as of this handover)

- **Dashboard**: gallery of Instagram posts across multiple accounts
  (chatgptricks, traselveloreal, and others added via the admin panel).
  Filters (account, search w/ `-word` exclusion, type, media, date,
  engagement, sort, page size) live in `FilterPopover` components. Search
  indexes captions, OCR'd cover text, and song/artist metadata.
- **Auth**: Firebase Google Sign-In, email allowlist enforced both
  client-side and server-side (`_require_firebase_user` middleware in
  `main.py`). Custom-token bootstrap (`/api/auth/custom-token`) lets the
  several subdomains/pages share one signed-in session. A legacy secondary
  password (`LEGACY_REFRESH_PASSWORD` in `App.jsx` /
  `TRICKS_DASH_REFRESH_PASSWORD` on the backend) is auto-supplied on admin
  POSTs — mostly redundant now that Firebase is the real boundary, but
  routes still check it; don't strip it without checking every admin route.
- **Settings command center**: full-page Admin/Dev app (not a Dashboard modal) with tabs for Overview, Accounts (add/
  activate/deactivate, per-account HOT-threshold suggestion based on a
  30-day-max rolling average of first-hour likes), Users (display names,
  operating roles, Slack IDs, admin access, and managed Sentient accounts),
  Usage, Notifications, System (disk, Apify, OCR), and Reports. The shared
  gear exposes Settings only to Admins and Devs; Dashboard remains focused on
  research and the post gallery.
- **Tracker**: daily follower-count snapshots (`account_snapshots` table),
  1d/7d/30d growth computed on **calendar-day boundaries** (collapse to each
  day's last snapshot, then diff day-over-day) — not a rolling 24h/7d/30h
  window, specifically so the headline growth number always matches the
  Historical Stats table's day rows. Drag-and-drop favorites, per-account and
  batch refresh buttons.
- **Queue**: a lightweight content-calendar/kanban. Cards are a fixed-aspect
  cover-image tile (`.queue-thumb-media`) with small overlays (drag handle,
  priority pill, hover-reveal edit pencil bottom-left); when the "Team
  overview" scope is active for an admin, an owner-email bar renders as a
  **separate flex row below** the image (never an overlay on top of it — an
  earlier version cropped the cover image doing this, since fixed).
  Multi-select "Assign to" is a `FilterPopover` dropdown, not an
  always-expanded checkbox list. Right-click context menu (move/remove).
  Posted tasks auto-hide after 24h.
- **Media-download modal** (`SlideDownload` in `postDetail.jsx`, shared by
  both dashboard and Queue): lists a post's carousel/video via
  `GET /api/dashboard/posts/media?...&list=1` (Apify-backed for large/
  uncached carousels — can take several seconds), lets you pick a subset or
  download all as a zip, has a per-cell solo-download and a Google Lens
  reverse-image-search icon (hover-revealed, not always visible). Column
  count is computed dynamically per item count (`bestColumns()`, 3–6 cols)
  to minimize empty last-row cells instead of a fixed CSS `auto-fill`.
  Per-cell skeleton shimmer while each image loads.
- **i18n / theming**: `prefs.js` / `prefsContext.jsx` exist with an ES
  dictionary and a `t()` helper (falls back to the English key if a
  translation is missing), and there's a light/dark theme toggle — but this
  is **partial**. Full i18n coverage and a proper light-theme contrast pass
  across all three pages (dashboard, tracker.html, insights.html) is
  unstarted backlog (see below).

## Repo map

**tricks-dash** (`src/`):
- `App.jsx` — the dashboard: filters, gallery, admin panel, everything not
  called out below.
- `postDetail.jsx` — shared cover/caption/stats panel + `SlideDownload`
  media modal, used by both `App.jsx`'s right rail and `queue.jsx`'s sidebar.
- `queue.jsx` / `queue.css` — Queue board.
- `api.js` — `API_BASE`, `apiFetch` (adds the Firebase ID token + legacy
  password header).
- `firebase.js` — Firebase app/init.
- `sso.js` — cross-subdomain custom-token bootstrap.
- `prefs.js` / `prefsContext.jsx` — i18n dictionary + `usePrefs()`.
- `styles.css` — every page's styling, one file.
- `data/traselveloreal-posts.json` + `traselveloreal-summary.json` — see
  "Second account" below; generated, not hand-edited.
- `smoke/` — a real smoke test: renders `<App />` in jsdom with Firebase/
  fetch stubbed, catches render-time crashes that `vite build` alone
  wouldn't (a past regression shipped a blank page that built cleanly). Run
  with `node smoke/run.mjs`.

**cortex** (`backend/app/`):
- `main.py` — ~60 routes: `/api/dashboard/*` (posts, queue, accounts, lists,
  media), `/api/admin/*` (accounts, users, usage, Apify ops, OCR, Slack,
  disk), `/api/tracker/*`, `/api/insights/*`, `/api/auth/custom-token`,
  `/api/health`.
- `db.py` — SQLite schema + queries (`predict.sqlite3` — see naming note
  above).
- `apify_sync.py` — Instagram scraping via Apify (profile scrape vs.
  per-URL scrape, engagement refresh rules, HOT detection).
- `slack_alerts.py` — Slack webhook notifications, including the free-form
  `notify_custom()`.
- `sentient_ocr.py` — thin client for the standalone Modal OCR worker
  (`workers/` in this repo) that reads text baked into cover images.
- `scheduler.py` — the hourly/daily background jobs (snapshot job, HOT
  sweep), with run-state persisted in a DB table (`scheduler_state`), not in
  memory — a deploy restarts the process, so anything tracked only in memory
  would silently re-run or lose its place.
- `config.py` — env var wiring, CORS origins, data dir. Read the comments
  here first; they explain most of the "why does this legacy name exist"
  questions.
- `render.yaml` — Render Blueprint: standard plan, 2GB disk at `/var/data`,
  `PREDICT_DATA_DIR`/`PREDICT_ALLOWED_ORIGINS`/`TRICKS_DASH_REFRESH_PASSWORD`/
  `SENTIENT_OCR_URL`/`SENTIENT_OCR_TOKEN` env vars.
- `APIFY_OPERATIONS_LEARNINGS.md` — **keep this one, it's still accurate.**
  Hard-won operational rules (in Spanish) with real dollar costs attached:
  never use `run-sync-get-dataset-items` for large scrapes, check existing
  Apify runs before re-scraping, never deploy mid-import, persist scheduler
  state in the DB not memory, verify live data after deploying (not just
  that it built), profile-scrape vs URL-scrape cost tradeoffs, etc. Read it
  before touching Apify, the scheduler, or the DB.

## Deploy workflow — read before pushing anything

Both repos' **mounted working copies on this Mac are not reliable git
state** — `git log` / `git status` there can show a HEAD behind origin and
uncommitted diffs, because the established pattern all session has been to
edit files on the mounted path directly, then copy them into a **fresh
scratch clone** for the actual commit/push. Don't `git reset`/`git pull` on
the mounted checkouts expecting to "fix" this; it's cosmetic, not data loss
— the real history lives on GitHub.

**Why a fresh clone, not the mounted path directly**: running `vite build`
or bulk `rsync` against the mounted (network) path throws a spurious
`Error: Unknown system error -35`. Always build from a real local clone.

**tricks-dash** (frontend):
1. `git clone https://github.com/chatgptricks/tricks-dash.git` into a
   scratch dir (or reuse one if it's already clean and up to date).
2. Copy over the files you changed from the mounted working copy.
3. `git diff --stat` — confirm *only* the files you meant to touch are
   dirty.
4. `npm install && npm run build` — must succeed with no errors.
5. Commit, push to `main` with the deploy token (see Secrets below).
6. **`main` is source only.** The live site is served from the separate
   `gh-pages` branch. Clone that branch separately and only ever touch:
   `index.html`, `queue.html`, and the hashed files under `assets/`
   (`dashboard-*.js/css`, `queue-*.js/css`, `postDetail-*.js`). Everything
   else on `gh-pages` (`tracker.html`, `insights.html`, favicons, `CNAME`,
   `traselveloreal-covers/`, `.nojekyll`) is unrelated static content —
   never touch it, and don't let an `rsync --delete` from a fresh `dist/`
   silently delete `.nojekyll` (it's not produced by `vite build`; re-touch
   it after syncing).
7. Verify live: `curl -s https://sentientdash.app/queue.html | grep -o 'assets/[a-zA-Z0-9._-]*'`
   and confirm the hashes match what you just built. An already-open browser
   tab can keep serving a stale cached bundle even after a real deploy —
   navigate with a `?cachebust=<anything>` query param to force a fresh
   fetch when verifying live.

**cortex** (backend): push to `main` — Render auto-deploys on push (no
separate gh-pages-style step). Confirm via
`curl -s https://cortex-api-db2e.onrender.com/api/health` — the `commit`
field should match your new commit hash. Takes ~1-2 minutes; expect
transient 502s during the swap.

**Never deploy cortex mid-import.** Account backfills/imports run as a
background thread; a Render redeploy restarts the process and silently
kills any in-progress import (this has actually happened, cost real Apify
spend, and lost partial data). There's no reliable way to check import
status without an authenticated session, so the pragmatic rule that's
worked all session: **just ask Esteban** ("is an import running right
now?") before pushing to cortex `main`. Frontend-only deploys never touch
the backend process and are always safe regardless of import state.

## Secrets

- Both repos have a git-ignored `.env.deploy` at their root with
  `GITHUB_DEPLOY_TOKEN=ghp_...` — the same token works for both repos' push
  access. Never commit it, never print its contents in a response.
- cortex has a Firebase service-account key
  (`sentient-dash-firebase-adminsdk-*.json`) at its root — also git-ignored
  (`*firebase-adminsdk*.json` in `.gitignore`), backend-only, never commit.
- Render env vars (set on the dashboard, not in `render.yaml`'s committed
  values): `PREDICT_ALLOWED_ORIGINS`, `TRICKS_DASH_REFRESH_PASSWORD`,
  `SENTIENT_OCR_URL`, `SENTIENT_OCR_TOKEN`.

## Second account: @traselveloreal (tricks-dash only)

A second Instagram account shown only in Sentient Dash, deliberately kept
**out** of cortex's shared Post DB/API — it's a standalone local dataset.
Canonical source: `traselveloreal-db/traselveloreal_posts.xlsx`. Bundled
into the frontend as static JSON (`src/data/traselveloreal-*.json`)
generated by `scripts/sync-traselveloreal.mjs` and imported directly into
`App.jsx` — there's no live API for this account, so new posts require
re-running the sync script and rebuilding/redeploying. See the (rewritten)
`README.md` for the exact commands.

## Known backlog (not started or not finished)

- Add the remaining subdomains to Firebase's authorized-domains list, then
  build/deploy/verify mobile login end-to-end (redirect-based sign-in was
  already switched in for mobile, but never fully verified live).
- Light theme + full i18n (ES/EN) across the dashboard, `tracker.html`, and
  `insights.html`, with toggles top-right and a contrast pass in both
  themes on all three pages. Bigger, multi-file effort; `prefs.js`/
  `prefsContext.jsx` exist but coverage is partial (mostly the media-modal
  and a handful of other strings so far).
- Two old backlog items ("Backend: listar media y zipear una selección" /
  "Modal de media con miniaturas y selección") described what is now the
  already-shipped, since-redesigned `SlideDownload` media-download modal —
  almost certainly stale duplicates at this point, worth a quick confirm
  with Esteban rather than assuming there's separate scope left there.

## Planned repo merge

Esteban is planning to merge tricks-dash and cortex into a single repo.
Things worth thinking through before doing it, based on what's load-bearing
today:
- **Two different deploy targets with different mechanisms**: tricks-dash
  ships a static build to a `gh-pages` branch (manual two-step: build in a
  clone, sync just the hashed assets + entry HTML into a separately-cloned
  `gh-pages` branch); cortex auto-deploys via Render's GitHub integration
  on push to `main`, using `render.yaml`. A monorepo needs to preserve both
  triggers independently (e.g. Render's root/build-path config pointed at a
  `backend/` subfolder, and a separate GitHub Actions or manual workflow
  still producing a `gh-pages`-equivalent for the frontend) — collapsing
  them into one branch/one deploy step would likely break one side.
- **Two separate GitHub remotes and deploy tokens today** (`chatgptricks/
  tricks-dash`, `chatgptricks/cortex`) — decide whether the merge keeps one
  as the survivor (rewriting the other's history in, or just copying its
  current tree over and dropping history) or creates a fresh third repo.
  Either way, update `.env.deploy` in whatever repo(s) remain and update the
  CORS origins / Firebase authorized domains if the Pages URL changes.
- `postDetail.jsx` is the one file that already spans "frontend talks to
  backend" concerns most tightly (it calls cortex's media/OCR/download
  endpoints directly) — a good file to sanity-check first after merging,
  since it's shared by two different pages already and easy to silently
  break for one of them while testing the other.
- Once merged, this handover only needs to exist once — fold the two copies
  into a single top-level `FOR_CODEX.md` instead of leaving them to drift
  apart.
