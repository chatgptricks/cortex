from __future__ import annotations

import importlib.util
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import TRIBEV2_CACHE_DIR, TRIBEV2_DEVICE, TRIBEV2_MODEL_ID


class TribeUnavailable(RuntimeError):
    pass


_MODEL: Any | None = None
_MODEL_LOCK = threading.Lock()


FEATURE_DEVICE = os.getenv("TRIBEV2_FEATURE_DEVICE", "cpu")
COVER_FEATURE_FREQUENCY = float(os.getenv("TRIBEV2_COVER_FEATURE_FREQUENCY", "1.0"))


NETWORK_PATTERNS: dict[str, list[str]] = {
    "visual": ["V1", "V2", "V3", "V4", "V6", "V7", "V8", "FFC", "PIT", "VVC", "LO", "MT", "MST", "PH", "DVT", "FST"],
    "attention": ["IPS", "FEF", "LIP", "VIP", "7", "8Av", "8C", "6a"],
    "language": ["IFSp", "IFSa", "44", "45", "STG", "STS", "TA", "A4", "A5"],
    "social": ["STS", "TPOJ", "TPJ", "TE", "PGi", "PGs"],
    "memory_scene": ["PHA", "RSC", "POS", "VMV", "PHT", "TF", "TG", "PCV"],
    "control": ["9", "a9", "p9", "46", "p32", "a24", "d32", "SCEF", "IFJ"],
    "motor": ["4", "3a", "3b", "1", "2", "6mp", "6ma", "55b"],
    "valuation": ["OFC", "10", "a10", "p10", "pOFC", "a24", "p32", "25"],
}


NETWORK_LABELS = {
    "visual": "Visual",
    "attention": "Attention",
    "language": "Language",
    "social": "Social",
    "memory_scene": "Memory/Scene",
    "control": "Executive control",
    "motor": "Sensorimotor",
    "valuation": "Valuation",
}


def tribe_status() -> dict[str, Any]:
    installed = importlib.util.find_spec("tribev2") is not None
    token_present = bool(os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN"))
    return {
        "installed": installed,
        "model_id": TRIBEV2_MODEL_ID,
        "device": TRIBEV2_DEVICE,
        "cache_dir": str(TRIBEV2_CACHE_DIR),
        "hf_token_present": token_present,
        "loaded": _MODEL is not None,
    }


def _load_model() -> Any:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from tribev2 import TribeModel
        except ImportError as exc:
            raise TribeUnavailable(
                "TRIBE v2 is not installed. Run `pip install -r backend/requirements.txt` "
                "inside the backend virtual environment."
            ) from exc

        try:
            _MODEL = TribeModel.from_pretrained(
                TRIBEV2_MODEL_ID,
                cache_folder=str(TRIBEV2_CACHE_DIR),
                device=TRIBEV2_DEVICE,
                config_update=_cpu_safe_config_update(),
            )
        except Exception as exc:
            raise TribeUnavailable(
                "Could not load TRIBE v2 from Hugging Face. Check that you accepted "
                "access to facebook/tribev2 and LLaMA 3.2, and that `HF_TOKEN` or "
                "`huggingface-cli login` is configured."
            ) from exc
        return _MODEL


def _cpu_safe_config_update() -> dict[str, Any]:
    """Override release config values that assume Meta's CUDA/SLURM cluster.

    The public TRIBE v2 checkpoint config pins feature extractors to `cuda`.
    On a local Mac/CPU PyTorch build that fails even if the main brain model is
    loaded on CPU. These are real TRIBE feature extractors, just forced onto a
    local device.
    """
    return {
        "data.text_feature.device": FEATURE_DEVICE,
        "data.audio_feature.device": FEATURE_DEVICE,
        "data.image_feature.image.device": FEATURE_DEVICE,
        "data.video_feature.image.device": FEATURE_DEVICE,
        "data.image_feature.use_audio": False,
        "data.video_feature.use_audio": False,
        "data.frequency": COVER_FEATURE_FREQUENCY,
        "data.num_workers": 0,
    }


def analyze_video(video_path: Path, duration_seconds: int | float | None = None) -> dict[str, Any]:
    model = _load_model()
    events = _cover_video_events(video_path, duration_seconds)
    preds, segments = model.predict(events=events, verbose=False)
    if preds.size == 0:
        raise TribeUnavailable("TRIBE v2 did not return analyzable segments for this video.")
    return summarize_predictions(preds, segments)


def _cover_video_events(video_path: Path, duration_seconds: int | float | None) -> pd.DataFrame:
    """Build visual-only events for covers converted into silent static videos.

    TRIBE's generic demo helper extracts/transcribes audio from videos. For this
    app, every uploaded cover is converted into a silent MP4, so running WhisperX
    on that audio is both slow and brittle on CPU/Mac. The real TRIBE v2 model
    still receives the video event; absent audio/text modalities are left absent.
    """
    from neuralset.events.transforms import ChunkEvents
    from neuralset.events.utils import standardize_events

    duration = float(duration_seconds) if duration_seconds else 8.0
    event = {
        "type": "Video",
        "filepath": str(video_path),
        "start": 0.0,
        "duration": max(2.0, duration),
        "timeline": "default",
        "subject": "default",
    }
    events = standardize_events(pd.DataFrame([event]))
    events = ChunkEvents(event_type_to_chunk="Video", max_duration=60, min_duration=30)(events)
    return standardize_events(events)


def _virality_potential(metrics: dict[str, Any], networks: dict[str, Any]) -> float:
    """
    Composite virality potential 0–1 from TRIBE v2 signals.
    Weights: social/valuation (sharing impulse), attention (scroll-stop),
    visual (aesthetic baseline), memory/scene (context depth), sustained ratio.
    """
    global_mean = max(float(metrics.get("global_mean_abs") or 0.0), 1e-9)

    def rel(key: str) -> float:
        raw = float((networks.get(key) or {}).get("raw") or 0.0)
        return min(1.0, raw / (global_mean * 1.5))

    score = (
        0.28 * rel("social")
        + 0.24 * rel("valuation")
        + 0.22 * rel("attention")
        + 0.12 * rel("visual")
        + 0.06 * rel("memory_scene")
        + 0.08 * min(1.0, float(metrics.get("sustained_ratio") or 0.0) * 2.0)
    )
    return round(min(1.0, max(0.0, score)), 3)


def summarize_predictions(preds: np.ndarray, segments: list[Any]) -> dict[str, Any]:
    values = np.nan_to_num(np.asarray(preds, dtype=float), copy=False)
    abs_values = np.abs(values)
    mean_abs_by_vertex = abs_values.mean(axis=0)
    mean_by_vertex = values.mean(axis=0)
    temporal_abs = abs_values.mean(axis=1)
    peak_by_time = abs_values.max(axis=1)

    first_half = temporal_abs[: max(1, len(temporal_abs) // 2)].mean()
    second_half = temporal_abs[len(temporal_abs) // 2 :].mean()
    split = values.shape[1] // 2
    left = float(mean_abs_by_vertex[:split].mean()) if split else 0.0
    right = float(mean_abs_by_vertex[split:].mean()) if split else 0.0
    balance_denominator = max(left + right, 1e-9)

    region_scores, roi_method, warnings = _region_scores(mean_abs_by_vertex)
    top_regions = _top_regions(region_scores)
    networks = _network_scores(region_scores)
    temporal_series = _temporal_series(temporal_abs, peak_by_time, segments)
    surface = _surface_activation(mean_abs_by_vertex)

    metrics = {
        "global_mean_abs": float(mean_abs_by_vertex.mean()),
        "global_peak_abs": float(abs_values.max()),
        "global_std": float(values.std()),
        "temporal_variability": float(temporal_abs.std()),
        "late_minus_early": float(second_half - first_half),
        "sustained_ratio": float(temporal_abs.mean() / max(float(peak_by_time.max()), 1e-9)),
        "left_right_balance": float((left - right) / balance_denominator),
        "n_segments": int(values.shape[0]),
        "n_vertices": int(values.shape[1]),
    }

    return {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": TRIBEV2_MODEL_ID,
        "mesh": "fsaverage5",
        "roi_method": roi_method,
        "virality_potential": _virality_potential(metrics, networks),
        "metrics": metrics,
        "top_regions": top_regions,
        "networks": networks,
        "temporal_series": temporal_series,
        "surface": surface,
        "warnings": warnings,
        "raw_stats": {
            "mean_min": float(mean_by_vertex.min()),
            "mean_max": float(mean_by_vertex.max()),
            "abs_p95": float(np.percentile(mean_abs_by_vertex, 95)),
        },
    }


def _surface_activation(mean_abs_by_vertex: np.ndarray, max_points: int | None = None) -> dict[str, Any]:
    values = np.asarray(mean_abs_by_vertex, dtype=float)
    if values.size == 0:
        return {"n_vertices": 0, "sample_indices": [], "values": [], "max": 0.0}
    stride = 1 if max_points is None else max(1, int(np.ceil(values.size / max_points)))
    sample_indices = np.arange(0, values.size, stride, dtype=int)
    sampled = values[sample_indices]
    peak = float(sampled.max()) if sampled.size else 0.0
    normalized = sampled / max(peak, 1e-9)
    return {
        "n_vertices": int(values.size),
        "sample_indices": sample_indices.tolist(),
        "values": [round(float(v), 4) for v in normalized],
        "max": peak,
    }


def _region_scores(mean_abs_by_vertex: np.ndarray) -> tuple[list[dict[str, Any]], str, list[str]]:
    warnings: list[str] = []
    try:
        import os
        mne_data = os.getenv("MNE_DATA")
        if mne_data:
            os.makedirs(mne_data, exist_ok=True)
            
        from tribev2.utils import get_hcp_labels

        labels = get_hcp_labels(mesh="fsaverage5", combine=False, hemi="both")
        regions = []
        for name, vertices in labels.items():
            vertex_index = np.asarray(vertices, dtype=int)
            vertex_index = vertex_index[vertex_index < mean_abs_by_vertex.shape[0]]
            if vertex_index.size == 0:
                continue
            raw = float(mean_abs_by_vertex[vertex_index].mean())
            regions.append({"name": name, "raw": raw})
        regions.sort(key=lambda item: item["raw"], reverse=True)
        return regions, "HCP-MMP1 on fsaverage5", warnings
    except Exception as exc:
        warnings.append(f"Could not load the HCP-MMP1 atlas; reporting vertices instead: {exc}")
        k = 12
        top = np.argsort(mean_abs_by_vertex)[::-1][:k]
        peak = float(mean_abs_by_vertex[top[0]]) if top.size else 1.0
        regions = [
            {
                "name": f"Vertex {int(index)}",
                "raw": float(mean_abs_by_vertex[index]),
                "score": round(100 * float(mean_abs_by_vertex[index]) / max(peak, 1e-9), 2),
            }
            for index in top
        ]
        return regions, "fsaverage5 vertices", warnings


def _top_regions(regions: list[dict[str, Any]], k: int = 12) -> list[dict[str, Any]]:
    ordered = sorted(regions, key=lambda item: item["raw"], reverse=True)[:k]
    peak = max((item["raw"] for item in ordered), default=1.0)
    return [
        {
            "name": item["name"],
            "raw": item["raw"],
            "score": round(100 * item["raw"] / max(peak, 1e-9), 2),
        }
        for item in ordered
    ]


def _network_scores(regions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    raw_scores: dict[str, list[float]] = {key: [] for key in NETWORK_PATTERNS}
    for region in regions:
        name = region["name"]
        for network, patterns in NETWORK_PATTERNS.items():
            if _matches_network_label(name, patterns):
                raw_scores[network].append(float(region["raw"]))

    averaged = {
        network: (sum(values) / len(values) if values else 0.0)
        for network, values in raw_scores.items()
    }
    peak = max(averaged.values()) if averaged else 1.0
    return {
        network: {
            "label": NETWORK_LABELS[network],
            "raw": raw,
            "score": round(100 * raw / max(peak, 1e-9), 2) if raw > 0 else 0.0,
        }
        for network, raw in averaged.items()
    }


def _matches_network_label(label: str, patterns: list[str]) -> bool:
    return any(label == pattern or label.startswith(pattern) for pattern in patterns)


def _temporal_series(
    temporal_abs: np.ndarray,
    peak_by_time: np.ndarray,
    segments: list[Any],
) -> list[dict[str, Any]]:
    series = []
    for index, value in enumerate(temporal_abs):
        segment = segments[index] if index < len(segments) else None
        start = float(getattr(segment, "offset", index)) if segment is not None else float(index)
        duration = float(getattr(segment, "duration", 1.0)) if segment is not None else 1.0
        series.append(
            {
                "index": index,
                "start": start,
                "duration": duration,
                "mean_abs": float(value),
                "peak_abs": float(peak_by_time[index]),
            }
        )
    return series


def write_analysis(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
