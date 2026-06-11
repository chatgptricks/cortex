#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

if [[ ! -d .venv ]]; then
  echo "Missing .venv. Run scripts/setup.sh first." >&2
  exit 1
fi

source .venv/bin/activate
PYTHONPATH=backend python scripts/prewarm_tribev2.py
