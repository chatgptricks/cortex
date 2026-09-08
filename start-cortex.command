#!/usr/bin/env bash
# Double-clickable launcher for Sentient Dash (backend :8000 + frontend :5173)
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

if [[ ! -d .venv ]]; then
  echo "Missing .venv. Run scripts/setup.sh first."
  read -r -p "Press Enter to close..."
  exit 1
fi

source .venv/bin/activate
mkdir -p data/logs

echo "Starting backend on http://127.0.0.1:8000 (logs: data/logs/backend.log)"
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 >> data/logs/backend.log 2>&1 &
API_PID=$!

cleanup() {
  kill "$API_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

sleep 3
if ! kill -0 "$API_PID" 2>/dev/null; then
  echo "BACKEND FAILED TO START — last log lines:"
  tail -30 data/logs/backend.log
fi

FRONTEND_DIR="${SENTIENT_FRONTEND_DIR:-../09 Tricks Dash/Tricks Dash}"
if [[ -d "$FRONTEND_DIR" ]]; then
  echo "Starting frontend from $FRONTEND_DIR"
  npm --prefix "$FRONTEND_DIR" run dev
else
  echo "Backend is running. Set SENTIENT_FRONTEND_DIR to start the frontend too."
  wait "$API_PID"
fi
