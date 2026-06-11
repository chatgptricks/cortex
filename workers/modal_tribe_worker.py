from __future__ import annotations

import os
import tempfile
from pathlib import Path

import modal
from fastapi import File, Form, Header, HTTPException, UploadFile


TRIBEV2_REPO = "tribev2 @ git+https://github.com/facebookresearch/tribev2.git@34f52344e5ba96660fac877393e1954e399d3ef3"
GPU_TYPE = os.getenv("MODAL_GPU", "L40S")
CPU_CORES = float(os.getenv("MODAL_CPU", "8"))
MEMORY_MB = int(os.getenv("MODAL_MEMORY_MB", "32768"))
TIMEOUT_SECONDS = int(os.getenv("MODAL_TIMEOUT", "1800"))
SCALEDOWN_WINDOW_SECONDS = int(os.getenv("MODAL_SCALEDOWN_WINDOW", "300"))
MAX_CONTAINERS = int(os.getenv("MODAL_MAX_CONTAINERS", "1"))


def _gpu_request() -> str | list[str]:
    gpu_options = [option.strip() for option in GPU_TYPE.split(",") if option.strip()]
    if not gpu_options:
        return "L40S"
    if len(gpu_options) == 1:
        return gpu_options[0]
    return gpu_options


cache_volume = modal.Volume.from_name(
    os.getenv("MODAL_VOLUME_NAME", "cortex-tribev2-cache"),
    create_if_missing=True,
)

image = (
    modal.Image.from_registry("nvidia/cuda:12.8.0-devel-ubuntu22.04", add_python="3.12")
    .apt_install("ffmpeg", "git")
    .env(
        {
            "HF_HOME": "/cache/huggingface",
            "MNE_DATA": "/cache/mne-data",
            "MNE_DATASETS_SAMPLE_PATH": "/cache/mne-data",
            "TRIBEV2_CACHE_DIR": "/cache/tribev2-cache",
            "TRIBEV2_DEVICE": "cuda",
            "TRIBEV2_FEATURE_DEVICE": "cuda",
            "TRIBEV2_COVER_FEATURE_FREQUENCY": "1.0",
            "PREDICT_DATA_DIR": "/tmp/cortex-data",
        }
    )
    .pip_install(
        "fastapi>=0.115,<1.0",
        "python-multipart>=0.0.9,<1.0",
        "pillow>=10,<13",
        "imageio-ffmpeg>=0.5,<1.0",
        "numpy==2.2.6",
        "nilearn>=0.12,<0.13",
        "torch==2.6.0",
        "torchvision==0.21.0",
        "torchaudio==2.6.0",
        "huggingface-hub>=0.31,<2.0",
        "paddleocr>=3.5,<3.6",
        "paddlepaddle==3.2.0",
        TRIBEV2_REPO,
    )
    .add_local_dir("backend/app", remote_path="/root/app")
)

app = modal.App("cortex-tribev2-worker", image=image)


@app.function(
    gpu=_gpu_request(),
    cpu=CPU_CORES,
    memory=MEMORY_MB,
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    max_containers=MAX_CONTAINERS,
    volumes={"/cache": cache_volume},
    secrets=[modal.Secret.from_name("cortex-secrets")],
)
@modal.fastapi_endpoint(method="POST", label="cortex-tribev2-analyze", docs=False)
async def analyze(
    file: UploadFile = File(...),
    duration_seconds: str = Form(""),
    authorization: str | None = Header(default=None),
) -> dict:
    expected_token = os.getenv("REMOTE_TRIBE_TOKEN")
    if expected_token and authorization != f"Bearer {expected_token}":
        raise HTTPException(status_code=401, detail="Invalid remote worker token.")

    duration = float(duration_seconds) if duration_seconds else None
    suffix = Path(file.filename or "cover.mp4").suffix or ".mp4"
    with tempfile.TemporaryDirectory() as temp_dir:
        video_path = Path(temp_dir) / f"cover{suffix}"
        video_path.write_bytes(await file.read())

        from app.tribe_adapter import analyze_video, tribe_status

        summary = analyze_video(video_path, duration_seconds=duration)
        status = tribe_status()

    await cache_volume.commit.aio()
    return {
        "summary": summary,
        "worker": {
            "provider": "modal",
            "gpu": GPU_TYPE,
            "tribev2": status,
        },
    }


@app.function(
    gpu=_gpu_request(),
    cpu=CPU_CORES,
    memory=MEMORY_MB,
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    max_containers=MAX_CONTAINERS,
    volumes={"/cache": cache_volume},
    secrets=[modal.Secret.from_name("cortex-secrets")],
)
@modal.fastapi_endpoint(method="POST", label="cortex-cover-ocr", docs=False)
async def ocr_batch(
    files: list[UploadFile] = File(...),
    crop_region: str = Form("lower_half"),
    authorization: str | None = Header(default=None),
) -> dict:
    expected_token = os.getenv("REMOTE_OCR_TOKEN") or os.getenv("REMOTE_TRIBE_TOKEN")
    if expected_token and authorization != f"Bearer {expected_token}":
        raise HTTPException(status_code=401, detail="Invalid remote OCR token.")
    if len(files) > 100:
        raise HTTPException(status_code=400, detail="OCR batch is limited to 100 files.")

    _prepare_paddle_cache()
    results = []
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        for index, file in enumerate(files):
            suffix = Path(file.filename or "cover.jpg").suffix or ".jpg"
            original_path = temp_path / f"{index:04d}{suffix}"
            cropped_path = temp_path / f"{index:04d}-crop.jpg"
            original_path.write_bytes(await file.read())
            _write_ocr_crop(original_path, cropped_path, crop_region)

            from app.ocr import extract_image_text

            results.append(
                {
                    "filename": file.filename,
                    "crop_region": crop_region,
                    "text": extract_image_text(cropped_path),
                }
            )

    await cache_volume.commit.aio()
    return {
        "results": results,
        "worker": {
            "provider": "modal",
            "gpu": GPU_TYPE,
            "ocr_engine": "paddle",
            "crop_region": crop_region,
        },
    }


def _prepare_paddle_cache() -> None:
    cache_path = Path("/cache/paddlex")
    target_path = Path("/root/.paddlex")
    cache_path.mkdir(parents=True, exist_ok=True)
    if target_path.exists() or target_path.is_symlink():
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.symlink_to(cache_path, target_is_directory=True)


def _write_ocr_crop(input_path: Path, output_path: Path, crop_region: str) -> None:
    from PIL import Image

    with Image.open(input_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        region = crop_region.lower().strip()
        if region in {"lower_half", "bottom_half", "hook"}:
            box = (0, height // 2, width, height)
        elif region in {"lower_60", "bottom_60"}:
            box = (0, int(height * 0.4), width, height)
        else:
            box = (0, 0, width, height)
        image.crop(box).save(output_path, format="JPEG", quality=95)
