# Deploying Cortex online

Frontend on GitHub Pages (free) + backend on Render (~$8/month). Analysis keeps
running on Modal GPU, so the server stays small.

## 1. Backend on Render

1. Push this repo to GitHub.
2. In Render: New → Blueprint → select the repo. `render.yaml` configures a
   Starter web service with a 2GB persistent disk mounted at `/var/data`.
3. Set the env vars Render asks for:
   - `PREDICT_API_KEY` — generate one: `openssl rand -hex 24`. This locks the API.
   - `PREDICT_ALLOWED_ORIGINS` — `https://YOUR_USER.github.io`
   - `HF_TOKEN`, `REMOTE_TRIBE_URL`, `REMOTE_TRIBE_TOKEN`, `REMOTE_OCR_URL` — same values as your local `.env`.
4. Upload your data once (DB + media). From your Mac, with the service running:
   the simplest path is Render's SSH: `render ssh cortex-api`, then `scp` (or use
   `rsync`) `data/predict.sqlite3`, `data/uploads/`, `data/videos/`, and
   `data/analyses/` into `/var/data/`. Alternatively start with an empty DB and
   re-import the Post DB export.
5. Check `https://cortex-api-XXXX.onrender.com/api/health` with header
   `X-API-Key: <your key>`.

## 2. Frontend on GitHub Pages

1. Repo Settings → Pages → Source: **GitHub Actions**.
2. Repo Settings → Secrets and variables → Actions:
   - Variable `VITE_API_BASE` = your Render URL (no trailing slash).
   - Secret `VITE_API_KEY` = the same `PREDICT_API_KEY` value.
3. Push to `main` (or run the "Deploy frontend to GitHub Pages" workflow
   manually). The site appears at `https://YOUR_USER.github.io/REPO/`.

## Notes

- Local dev is unchanged: without `PREDICT_API_KEY` set, no auth is enforced and
  the frontend talks to `127.0.0.1:8000` as before.
- The API key ends up embedded in the public frontend bundle — it protects
  against strangers, not against someone you share the URL with. Treat the
  Pages URL itself as semi-private.
- Server installs `backend/requirements-server.txt` (no tribev2/torch); local
  GPU analysis still uses `backend/requirements.txt`.
- First boot after this update strips brain-surface arrays from historical DB
  rows and VACUUMs: the DB shrinks ~630MB → ~40MB (one-time, takes a minute).
