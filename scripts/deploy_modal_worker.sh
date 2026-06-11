#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

if [[ -d .venv ]]; then
  source .venv/bin/activate
fi

if ! command -v modal >/dev/null 2>&1 && [[ -d .modal-venv ]]; then
  source .modal-venv/bin/activate
fi

MODAL_GPU="${MODAL_GPU:-L40S}"
MODAL_CPU="${MODAL_CPU:-8}"
MODAL_MEMORY_MB="${MODAL_MEMORY_MB:-32768}"
MODAL_TIMEOUT="${MODAL_TIMEOUT:-1800}"
MODAL_SCALEDOWN_WINDOW="${MODAL_SCALEDOWN_WINDOW:-300}"
MODAL_MAX_CONTAINERS="${MODAL_MAX_CONTAINERS:-1}"
export MODAL_GPU MODAL_CPU MODAL_MEMORY_MB MODAL_TIMEOUT MODAL_SCALEDOWN_WINDOW MODAL_MAX_CONTAINERS

if ! command -v modal >/dev/null 2>&1; then
  echo "Modal CLI is not installed. Run: scripts/setup_modal_cli.sh" >&2
  exit 1
fi

echo "Deploying Modal TRIBE worker:"
echo "  MODAL_GPU=$MODAL_GPU"
echo "  MODAL_CPU=$MODAL_CPU"
echo "  MODAL_MEMORY_MB=$MODAL_MEMORY_MB"
echo "  MODAL_TIMEOUT=$MODAL_TIMEOUT"
echo "  MODAL_SCALEDOWN_WINDOW=$MODAL_SCALEDOWN_WINDOW"
echo "  MODAL_MAX_CONTAINERS=$MODAL_MAX_CONTAINERS"

modal deploy workers/modal_tribe_worker.py
