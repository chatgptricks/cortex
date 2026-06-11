# Cortex by Sentient

Local app for analyzing Instagram covers with real TRIBE v2, saving Post DB covers with real likes, and comparing A/B cover candidates.

## What It Does

- Converts a cover image into a real MP4 with repeated frames and silent audio.
- Runs `facebook/tribev2` to estimate fMRI activation on `fsaverage5`.
- Summarizes HCP-MMP1 regions, approximate brain networks derived from those regions, temporal activity, and cortical surface activation.
- Stores previous posts with actual likes and trains a local calibration model to estimate likes.
- Compares A/B covers, chooses a winner automatically, and uses calibrated likes when enough Post DB data exists; otherwise it falls back to global TRIBE v2 activation and marks the result as uncalibrated.
- Generates an on-demand natural-language report for completed Analyze/A-B results through a configurable LLaMA chat model on Hugging Face Inference Providers. Post DB keeps structured brain data only.

## Real Requirements

TRIBE v2 uses Hugging Face weights and a gated LLaMA 3.2 text encoder. Before running analyses:

1. Accept access to [`facebook/tribev2`](https://huggingface.co/facebook/tribev2).
2. Accept access to the LLaMA 3.2 model required by TRIBE v2 if Hugging Face requests it.
3. Create a `.env` file with a read token:

```bash
HF_TOKEN=...
```

You can also use `huggingface-cli login` inside the virtualenv.

LLM reports use the same Hugging Face token. By default the app calls `meta-llama/Meta-Llama-3.1-8B-Instruct` through provider `featherless-ai`, because that route works through Hugging Face Inference Providers with this account. You can override that in `.env`:

```bash
LLM_REPORT_MODEL_ID=meta-llama/Meta-Llama-3.1-8B-Instruct
LLM_REPORT_PROVIDER=featherless-ai
```

TRIBE v2 is published under CC BY-NC 4.0. If this app will be used commercially, review that license before putting it into production.

Historical cover OCR is intentionally batch-only. Imports and uploads do not run OCR. After at least `100` Post DB covers have completed TRIBE v2 analysis and still have empty OCR text, run the Modal OCR batch job. The Modal worker crops each cover to the lower half before OCR, so the model reads the hook area instead of background logos or scenery.

## Install

TRIBE v2/PyTorch requires Python 3.11 or 3.12. On this Mac, if the global `python3` is not compatible, pass an explicit path:

```bash
PYTHON_BIN=/Users/tbnalfaro/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/setup.sh
```

On a machine with `python3.12` or `python3.11`:

```bash
scripts/setup.sh
```

## Development

```bash
scripts/dev.sh
```

Open:

- Frontend: http://127.0.0.1:5173
- Backend: http://127.0.0.1:8000/api/health

The first analysis can take a while because it downloads weights, extracts features, and caches results. For static covers, the app uses 2-second videos by default and forces visual features to 1 Hz to avoid repeating work on the same image.

## UI Scope

This app is a desktop-first internal tool. Do not spend implementation or QA time optimizing mobile layouts unless the user explicitly asks for mobile work. Default frontend validation should target the local desktop browser workflow.

## Remote GPU Worker

The backend can send cover videos to a remote TRIBE worker instead of running TRIBE locally. If `REMOTE_TRIBE_URL` is empty, the app stays in local mode.

For Modal:

```bash
scripts/setup_modal_cli.sh
.modal-venv/bin/modal token new
.modal-venv/bin/modal secret create cortex-secrets HF_TOKEN=hf_... REMOTE_TRIBE_TOKEN=make-a-long-random-token
MODAL_GPU=L40S scripts/deploy_modal_worker.sh
```

After Modal prints the endpoint URLs, add them to `.env`:

```bash
REMOTE_TRIBE_URL=https://your-workspace--cortex-tribev2-analyze.modal.run
REMOTE_TRIBE_TOKEN=the-same-long-random-token
REMOTE_TRIBE_TIMEOUT=1800
REMOTE_OCR_URL=https://your-workspace--cortex-cover-ocr.modal.run
REMOTE_OCR_TOKEN=the-same-long-random-token
REMOTE_OCR_TIMEOUT=1800
OCR_BATCH_MIN_READY=100
OCR_BATCH_SIZE=100
OCR_CROP_REGION=lower_half
```

Restart `scripts/dev.sh`. Health should show `Remote GPU enabled`; new analyses will upload the generated MP4 to Modal and save the returned TRIBE summary in the same local database.

To run OCR once a full batch is ready:

```bash
.import-venv/bin/python scripts/run_modal_ocr_batch.py --dry-run
.import-venv/bin/python scripts/run_modal_ocr_batch.py
```

The script refuses to run until there are at least `OCR_BATCH_MIN_READY` historical posts with `status='completed'`, saved TRIBE data, and blank OCR text.

The worker defaults are tuned for lower-cost GPU runs:

```bash
MODAL_GPU=L40S
MODAL_CPU=8
MODAL_MEMORY_MB=32768
MODAL_TIMEOUT=1800
MODAL_SCALEDOWN_WINDOW=300
MODAL_MAX_CONTAINERS=1
```

`L40S` is the default for this project because it keeps the GPU hourly rate much lower. If a specific run needs more speed, set `MODAL_GPU=H100` only for that deploy. `MODAL_MAX_CONTAINERS=1` prevents accidental parallel GPU spend.

Modal's listed GPU prices checked on May 24, 2026:

| GPU | Approx. hourly GPU-only cost | Approx. $500 GPU-only hours |
| --- | ---: | ---: |
| L40S | $1.95/hr | 256 hr |
| H100 | $3.95/hr | 126 hr |
| H200 | $4.54/hr | 110 hr |
| B200 | $6.25/hr | 80 hr |

CPU and RAM are billed separately, so the configured `8` CPU cores and `32 GiB` memory reduce those hour estimates slightly. Set a Modal workspace budget in the Modal billing dashboard so the card cannot run past the $500 cap.

## Prewarm Downloads

To download and validate weights before analyzing covers:

```bash
scripts/prewarm.sh
```

This uses `HF_TOKEN` from `.env` and leaves the models in the local Hugging Face cache. It does not replace per-cover analysis, but it avoids repeating large downloads.
