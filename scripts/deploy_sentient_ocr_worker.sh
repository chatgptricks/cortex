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

if ! command -v modal >/dev/null 2>&1; then
  echo "Modal CLI is not installed. Run: scripts/setup_modal_cli.sh" >&2
  exit 1
fi

echo "One-time setup, if you haven't already:"
echo "  1. Create a Modal secret named 'sentient-ocr-secret' with key SENTIENT_OCR_TOKEN"
echo "     (any string you like -- it just needs to match SENTIENT_OCR_TOKEN in Render's env)."
echo "     modal secret create sentient-ocr-secret SENTIENT_OCR_TOKEN=<your-token>"
echo "  2. Set on Render (cortex-api service): SENTIENT_OCR_URL and SENTIENT_OCR_TOKEN"
echo "     (the URL is printed by this deploy once it finishes -- something like"
echo "     https://<workspace>--sentient-cover-ocr.modal.run)."
echo ""
echo "Deploying Sentient Dash OCR worker (no GPU, RapidOCR, full-image only):"

modal deploy workers/modal_ocr_worker.py
