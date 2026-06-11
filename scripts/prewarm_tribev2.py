from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(os.getenv("TRIBEV2_CACHE_DIR", ROOT / "data" / "tribev2-cache"))


def main() -> None:
    token = os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is not configured. Check /Users/tbnalfaro/Documents/Predict/.env")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("Downloading facebook/tribev2 checkpoint and config...")
    hf_hub_download("facebook/tribev2", "config.yaml", token=token)
    hf_hub_download("facebook/tribev2", "best.ckpt", token=token)

    print("Downloading the main visual model used by TRIBE v2...")
    snapshot_download("facebook/vjepa2-vitg-fpc64-256", token=token)

    print("Downloading the gated text encoder in case captions/audio are enabled later...")
    snapshot_download("meta-llama/Llama-3.2-3B", token=token)

    print("Loading TRIBE v2 once to validate weights and the local cache...")
    from app.tribe_adapter import _load_model, tribe_status

    _load_model()
    print("TRIBE v2 ready.")
    print(tribe_status())


if __name__ == "__main__":
    main()
