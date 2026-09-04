# Stability and operating-cost review — 2026-09-04

## Release changes

- Firebase token verification, permission lookups, time-zone updates and usage writes leave the asynchronous HTTP event loop and execute in Starlette's bounded worker pool. Permission checks still run on every request; no permission cache was added.
- Queue SSE subscribers share a database revision snapshot for 750 ms per web process. Database I/O executes outside the event loop, and a lock coalesces simultaneous cache misses. Ordinary Queue reads still query their transaction directly. Forty concurrent subscribers are covered by a regression test that performs one read, not forty.
- Hourly/daily ingestion slots use a conditional database upsert. Only the process that commits the claim runs the slot. Database read/write failures stop the tick instead of masquerading as missing state and repeating paid ingestion. Older slots cannot replace newer claims.
- Scheduler shutdown retains ownership of a thread that has not finished. Restarting it cannot clear the stop signal and create a second scheduler alongside the first. The dedicated scheduler also honors the global scheduler disable flag.
- Recurring R2 batches no longer rerun schema migrations. CLI invocation still initializes the database. One upload exception does not discard the remainder of the batch. Media references are updated only if unchanged since selection, protecting concurrent cover/avatar replacements.
- The companion Tricks Dash frontend removes abort listeners after retry waits, adds bounded jittered retry delays, releases SSE readers and progressively delays broken-stream reconnections up to 30 seconds. Unauthorized streams stop rather than continually retrying. Non-idempotent mutations remain single-attempt.

## Verification

Backend: `PYTHONPATH=backend ./.venv/bin/python -m pytest backend/tests -q`.

Frontend, from the Tricks Dash checkout:

```sh
npm run test:resilience
npm run test:queue
VITE_API_BASE=https://cortex-api-db2e.onrender.com VITE_SKIP_PUBLIC=1 npm run build
```

The runtime tests use isolated SQLite databases and mocked identity/media services. They do not scrape Apify, send notifications, modify production data, or prove production Postgres failover behavior. The conditional upsert uses SQL supported by the existing SQLite/Postgres compatibility layer; a production verification is still required after release.

Results: 53 backend tests passed; frontend retry/API resilience checks, Queue planner checks and production build passed. Dashboard, Queue, Settings, mobile, roles, favicons, avatars, Insights helpers, static pages and import wizard smoke checks passed. The Queue test now uses a fixed clock so its 09:10 drag target is not rejected as being in the past later in the day. The Settings test opens the user card gear before checking administrative controls, matching the pending compact-card redesign included in this release. Vite reports existing classic-script bundling warnings, and FastAPI warns about the existing deprecated startup hook.

The live health endpoint was reachable and reported ready on commit `0675d563fb8f6ca60437b8cf754e456be7163059` during the review. That is the existing deployment, not these local changes. A healthy liveness response alone does not prove authenticated requests or background jobs are healthy.

## Remaining architecture constraints

The checked-in Render Blueprint still attaches a 50 GB disk to the web service. Render documents that attached persistent disks disable zero-downtime deployments: https://render.com/docs/disks and https://render.com/docs/deploys. Do not remove this disk based on R2 configuration alone: verify every legacy media reference, attachment, fallback write and database dependency first, and retain a recoverable backup.

The code already separates scheduled work from the web process by default. Actual worker provisioning, enabled flags and deployment ownership have not been verified in Render during this review. Do not turn on a second scheduler or move traffic based only on the Blueprint. Atomic slot claims prevent duplicate execution of the same slot, not overlapping different slots or recovery of an interrupted import. Claims deliberately retain the existing at-most-once behavior: a crash after claiming can skip the unfinished work. Durable job checkpoints and worker ownership/leases are still needed for resumable imports and controlled deployments.

R2 backfill still scans legacy references from the start of each source. A sufficiently large block of missing/unrecoverable local files can starve later records. Resolve missing files or add a durable, fair scan cursor with explicit retry policy before declaring migration complete. Never delete old media to force the counter to zero.

No paid plans, cloud services, schedules or retention policies were changed. Fewer duplicate reads/requests and avoiding repeated paid slots reduce unnecessary work, but do not by themselves lower a fixed Render subscription. No billing was inspected, and no monthly savings are claimed.

## Release gate

The user authorized publishing all pending application changes, including `src/App.jsx`, `src/styles.css`, scheduler lifecycle support and the worker entrypoint. The worker entrypoint is source preparation only: this release does not provision or enable another service. Duplicate backup files and credentials are excluded. Authenticated preflight checks reported both account backfill and OCR as not running before release preparation.

Before committing/publishing, confirm no production import is running and obtain release authorization. Select only the intended changes; include/review the scheduler's pre-existing lifecycle dependencies explicitly. Publish the backend, wait for its exact commit in health, then verify authenticated Dashboard/Queue reads, stream recovery and permission boundaries. Publish frontend static assets without removing old hashed bundles or unrelated Pages files. Never restart the backend during an import just to validate a release.
