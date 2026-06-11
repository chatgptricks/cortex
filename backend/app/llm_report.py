from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from .config import (
    LLM_REPORT_MAX_TOKENS,
    LLM_REPORT_MODEL_ID,
    LLM_REPORT_PROVIDER,
    LLM_REPORT_TEMPERATURE,
    LLM_REPORT_TIMEOUT,
)


class LlmReportUnavailable(RuntimeError):
    pass


SYSTEM_PROMPT = """You write decision-useful creative reports for Cortex by Sentient.

Use only the supplied TRIBE v2 analysis data. TRIBE v2 estimates an fMRI-like cortical response from the cover media; it is not a literal measurement of a real viewer's brain. Do not diagnose, overclaim, or invent audience demographics.

Write in English. Be direct, concrete, and useful for choosing Instagram cover artwork.
Do not use raw decimals or JSON field names. Use only whole 0-100 scores, named networks, named regions, and the provided temporal profile. Do not infer emotions, intent, demographics, or medical meaning. Do not describe an actual viewer's brain or cortex; say "TRIBE v2 estimates" or "the model estimates" instead. neural_archive_percentile is only a TRIBE activation comparison against the archive, not evidence of past performance. If calibrated_prediction is null, the Performance implication section must start exactly with "Performance cannot be estimated yet because local calibration is not ready." Do not use "perform well", "expected", or "likely" when calibrated_prediction is null.

Format exactly:
Cortex Report
Overall read: ...
Neural signal: ...
Timing: ...
Performance implication: ...
Creative next moves:
- ...
- ...
- ...
Calibration note: ...
"""


def llm_report_status() -> dict[str, Any]:
    return {
        "model_id": LLM_REPORT_MODEL_ID,
        "provider": _provider_for_status(),
        "hf_token_present": bool(_hf_token()),
    }


def generate_llm_report(post: dict[str, Any], calibration: dict[str, Any] | None = None) -> dict[str, Any]:
    if not post.get("analysis_summary"):
        raise LlmReportUnavailable("This post needs a completed TRIBE v2 analysis before an LLM report can be generated.")

    token = _hf_token()
    if not token:
        raise LlmReportUnavailable("HF_TOKEN or HUGGING_FACE_HUB_TOKEN is required to generate LLaMA reports.")

    try:
        from huggingface_hub import InferenceClient
    except ImportError as exc:
        raise LlmReportUnavailable(
            "huggingface_hub is not installed. Run `pip install -r backend/requirements.txt` in the backend virtualenv."
        ) from exc

    client = InferenceClient(
        model=LLM_REPORT_MODEL_ID,
        provider=_provider_for_client(),
        token=token,
        timeout=LLM_REPORT_TIMEOUT,
    )
    payload = _report_payload(post, calibration)

    try:
        output = client.chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Create the report from this JSON data:\n{json.dumps(payload, indent=2)}"},
            ],
            max_tokens=LLM_REPORT_MAX_TOKENS,
            temperature=LLM_REPORT_TEMPERATURE,
        )
    except Exception as exc:
        raise LlmReportUnavailable(f"LLM report generation failed: {exc}") from exc

    report_text = _extract_message_content(output).strip()
    if not report_text:
        raise LlmReportUnavailable("The LLM returned an empty report.")

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": LLM_REPORT_MODEL_ID,
        "provider": _provider_for_status(),
        "report": report_text,
    }


def _report_payload(post: dict[str, Any], calibration: dict[str, Any] | None) -> dict[str, Any]:
    summary = post["analysis_summary"]
    metrics = summary.get("metrics") or {}
    networks = summary.get("networks") or {}
    ranked_networks = sorted(
        [
            {
                "key": key,
                "label": network.get("label", key),
                "score_0_to_100": round(float(network.get("score") or 0.0)),
            }
            for key, network in networks.items()
        ],
        key=lambda item: item["score_0_to_100"],
        reverse=True,
    )
    temporal_series = summary.get("temporal_series") or []
    temporal_profile = _temporal_profile(metrics, temporal_series)

    return {
        "cover": {
            "title": post.get("title"),
            "caption_or_notes": post.get("caption"),
            "published_at": post.get("published_at"),
            "actual_likes": post.get("likes"),
        },
        "prediction": {
            "calibrated_prediction": post.get("calibrated_prediction"),
            "neural_archive_percentile": post.get("tribe_percentile"),
            "calibration": calibration,
            "performance_rule": _performance_rule(post),
        },
        "tribe_v2": {
            "model": summary.get("model"),
            "mesh": summary.get("mesh"),
            "roi_method": summary.get("roi_method"),
            "temporal_profile": temporal_profile,
            "hemisphere_balance": _hemisphere_balance(metrics.get("left_right_balance")),
            "ranked_networks": ranked_networks,
            "top_regions": [
                {
                    "name": region.get("name"),
                    "score_0_to_100": round(float(region.get("score") or 0.0)),
                }
                for region in (summary.get("top_regions") or [])[:8]
            ],
            "warnings": summary.get("warnings") or [],
        },
    }


def _performance_rule(post: dict[str, Any]) -> str:
    if post.get("calibrated_prediction"):
        return "A calibrated prediction exists, so performance can be discussed as a model estimate, not a guarantee."
    return "Performance cannot be estimated yet because local calibration is not ready. Discuss signals only as creative hypotheses to test."


def _temporal_profile(metrics: dict[str, Any], temporal_series: list[dict[str, Any]]) -> dict[str, Any]:
    mean_activation = max(float(metrics.get("global_mean_abs") or 0.0), 1e-9)
    late_shift = float(metrics.get("late_minus_early") or 0.0) / mean_activation
    sustained_ratio = float(metrics.get("sustained_ratio") or 0.0)
    if late_shift > 0.12:
        label = "builds over time"
    elif late_shift < -0.12:
        label = "front-loaded"
    elif sustained_ratio >= 0.18:
        label = "sustained"
    else:
        label = "peak-driven"

    strongest_segment = None
    if temporal_series:
        strongest = max(temporal_series, key=lambda item: float(item.get("mean_abs") or 0.0))
        start = int(round(float(strongest.get("start") or 0.0)))
        duration = int(round(float(strongest.get("duration") or 0.0)))
        strongest_segment = f"{_ordinal_segment(int(strongest.get('index') or 0))}; starts around {start}s and lasts about {duration}s"

    return {
        "label": label,
        "segments": int(metrics.get("n_segments") or len(temporal_series) or 0),
        "strongest_segment": strongest_segment,
    }


def _ordinal_segment(index: int) -> str:
    if index == 0:
        return "first segment"
    if index == 1:
        return "second segment"
    if index == 2:
        return "third segment"
    return f"segment {index + 1}"


def _hemisphere_balance(value: Any) -> str:
    balance = float(value or 0.0)
    if balance > 0.08:
        return "left-weighted"
    if balance < -0.08:
        return "right-weighted"
    return "balanced"


def _extract_message_content(output: Any) -> str:
    choices = getattr(output, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        return _normalize_content(content)

    if isinstance(output, dict):
        choices = output.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return _normalize_content(message.get("content"))
    return _normalize_content(output)


def _normalize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _hf_token() -> str | None:
    return os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")


def _provider_for_client() -> str | None:
    provider = LLM_REPORT_PROVIDER.strip()
    if not provider or provider.lower() in {"none", "default"}:
        return None
    return provider


def _provider_for_status() -> str:
    provider = _provider_for_client()
    return provider or "default"
