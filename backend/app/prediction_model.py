from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .calibration import (
    FEATURE_ORDER,
    _build_hook_vocab,
    _build_vocab,
    _metadata_items,
    _standardize,
    _training_likes,
    _validation_indices,
    feature_vector,
)
from .config import MIN_CALIBRATION_SAMPLES


MODEL_VERSION = "advanced_temporal_v1"
PROBABILITY_KEYS = {
    "median": "probability_above_median",
    "p75": "probability_above_p75",
    "p90": "probability_above_p90",
}


@dataclass
class AdvancedPredictionModel:
    ready: bool
    sample_count: int
    feature_order: list[str]
    model_version: str = MODEL_VERSION
    vocab_tags: list[str] | None = None
    vocab_person: list[str] | None = None
    vocab_company: list[str] | None = None
    vocab_post_type: list[str] | None = None
    vocab_hook_tokens: list[str] | None = None
    means: list[float] | None = None
    scales: list[float] | None = None
    coefficients: list[float] | None = None
    intercept: float | None = None
    ridge_alpha: float | None = None
    ridge_weight: float | None = None
    validation_strategy: str | None = None
    validation_count: int | None = None
    mae_validation: float | None = None
    log_mae_validation: float | None = None
    wape_validation: float | None = None
    r2_log_validation: float | None = None
    spearman_validation: float | None = None
    interval_log_q80: float | None = None
    interval_log_q90: float | None = None
    train_median_likes: float | None = None
    train_p75_likes: float | None = None
    train_p90_likes: float | None = None
    train_p95_likes: float | None = None
    train_p99_likes: float | None = None
    train_max_likes: float | None = None
    global_log_baseline: float | None = None
    post_type_log_baselines: dict[str, float] | None = None
    validation_predicted_logs: list[float] | None = None
    validation_likes: list[float] | None = None
    probability_priors: dict[str, float] | None = None
    message: str | None = None


def fit_advanced_prediction(posts: list[dict[str, Any]]) -> AdvancedPredictionModel:
    training_rows = [
        post
        for post in posts
        if post.get("section") == "historical"
        and post.get("status") == "completed"
        and post.get("analysis_summary")
        and post.get("likes") is not None
    ]
    sample_count = len(training_rows)
    if sample_count < MIN_CALIBRATION_SAMPLES:
        return AdvancedPredictionModel(
            ready=False,
            sample_count=sample_count,
            feature_order=FEATURE_ORDER,
            message=f"{MIN_CALIBRATION_SAMPLES - sample_count} analyzed Post DB posts still needed.",
        )

    vocab_tags = _build_vocab(training_rows, "tags")
    vocab_person = _build_vocab(training_rows, "person_label")
    vocab_company = _build_vocab(training_rows, "company_label")
    vocab_post_type = _build_vocab(training_rows, "post_type_label")
    vocab_hook_tokens = _build_hook_vocab(training_rows)
    feature_order = _feature_order(vocab_tags, vocab_person, vocab_company, vocab_post_type, vocab_hook_tokens)

    stub = AdvancedPredictionModel(
        ready=False,
        sample_count=sample_count,
        feature_order=feature_order,
        vocab_tags=vocab_tags,
        vocab_person=vocab_person,
        vocab_company=vocab_company,
        vocab_post_type=vocab_post_type,
        vocab_hook_tokens=vocab_hook_tokens,
    )

    x = np.nan_to_num(
        np.array([feature_vector(row, stub) for row in training_rows], dtype=float),
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    )
    x = np.clip(x, -1e6, 1e6)
    y_likes = np.array([float(_training_likes(row)) for row in training_rows], dtype=float)
    y_log = np.log1p(y_likes)
    train_idx, val_idx, validation_strategy = _validation_indices(training_rows)

    best = _select_model(
        x=x,
        y_log=y_log,
        y_likes=y_likes,
        rows=training_rows,
        train_idx=train_idx,
        val_idx=val_idx,
    )

    x_scaled, means, scales = _standardize(x)
    beta = _fit_ridge(x_scaled, y_log, alpha=best["alpha"])
    group_baselines = _post_type_log_baselines(training_rows, y_log)
    global_log_baseline = float(np.median(y_log))
    validation_predicted_logs = best["validation_predicted_logs"]
    validation_likes = y_likes[val_idx]
    thresholds = _thresholds(y_likes)
    probability_priors = {
        key: float(np.mean(y_likes >= threshold))
        for key, threshold in thresholds.items()
    }
    residual_logs = np.abs(y_log[val_idx] - validation_predicted_logs)

    stub.ready = True
    stub.means = means.tolist()
    stub.scales = scales.tolist()
    stub.intercept = float(beta[0])
    stub.coefficients = beta[1:].tolist()
    stub.ridge_alpha = float(best["alpha"])
    stub.ridge_weight = float(best["ridge_weight"])
    stub.validation_strategy = validation_strategy
    stub.validation_count = int(len(val_idx))
    stub.mae_validation = float(best["mae"])
    stub.log_mae_validation = float(best["log_mae"])
    stub.wape_validation = float(best["wape"])
    stub.r2_log_validation = float(best["r2_log"]) if best["r2_log"] is not None else None
    stub.spearman_validation = float(best["spearman"]) if best["spearman"] is not None else None
    stub.interval_log_q80 = float(np.percentile(residual_logs, 80))
    stub.interval_log_q90 = float(np.percentile(residual_logs, 90))
    stub.train_median_likes = float(np.percentile(y_likes, 50))
    stub.train_p75_likes = float(np.percentile(y_likes, 75))
    stub.train_p90_likes = float(np.percentile(y_likes, 90))
    stub.train_p95_likes = float(np.percentile(y_likes, 95))
    stub.train_p99_likes = float(np.percentile(y_likes, 99))
    stub.train_max_likes = float(np.max(y_likes))
    stub.global_log_baseline = global_log_baseline
    stub.post_type_log_baselines = group_baselines
    stub.validation_predicted_logs = validation_predicted_logs.tolist()
    stub.validation_likes = validation_likes.tolist()
    stub.probability_priors = probability_priors
    stub.message = (
        "Advanced prediction model trained with temporal validation: tuned ridge on log-likes, "
        "post-type baseline blending, conformal intervals, and calibrated top-percentile probabilities."
    )
    return stub


def predict_performance(post: dict[str, Any], model: AdvancedPredictionModel) -> dict[str, Any] | None:
    if not model.ready or not post.get("analysis_summary"):
        return None
    if not model.means or not model.scales or model.intercept is None or not model.coefficients:
        return None

    x = np.array(feature_vector(post, model), dtype=float)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x = np.clip(x, -1e6, 1e6)
    means = np.array(model.means, dtype=float)
    scales = np.array(model.scales, dtype=float)
    coefficients = np.array(model.coefficients, dtype=float)
    ridge_log = float(model.intercept + ((x - means) / scales) @ coefficients)
    baseline_log = _baseline_for_post(post, model)
    ridge_weight = float(model.ridge_weight if model.ridge_weight is not None else 1.0)
    predicted_log = (ridge_weight * ridge_log) + ((1.0 - ridge_weight) * baseline_log)
    predicted_log = float(np.clip(predicted_log, 0.0, math.log1p(max(float(model.train_max_likes or 0.0), 1.0)) + 0.35))
    predicted = max(0.0, math.expm1(predicted_log))

    q80 = float(model.interval_log_q80 or 0.45)
    q90 = float(model.interval_log_q90 or q80)
    prediction_low = max(0.0, math.expm1(predicted_log - q80))
    prediction_high = max(predicted, math.expm1(predicted_log + q80))
    prediction_low_wide = max(0.0, math.expm1(predicted_log - q90))
    prediction_high_wide = max(predicted, math.expm1(predicted_log + q90))

    thresholds = {
        "median": float(model.train_median_likes or predicted),
        "p75": float(model.train_p75_likes or predicted),
        "p90": float(model.train_p90_likes or predicted),
    }
    probabilities = {
        PROBABILITY_KEYS[key]: _probability_above(predicted_log, threshold, model)
        for key, threshold in thresholds.items()
    }
    sample_confidence = min(0.95, max(0.25, math.log10(model.sample_count + 1) / 3.2))
    error_confidence = 1.0 / (1.0 + max(0.0, float(model.log_mae_validation or 0.0)))
    rank_signal = (
        0.45 * _percentile_from_prediction(predicted, model)
        + 0.35 * probabilities["probability_above_p75"]
        + 0.20 * probabilities["probability_above_p90"]
    )
    confidence = min(0.92, max(0.2, sample_confidence * error_confidence))

    return {
        "predicted_likes": round(predicted),
        "prediction_low": round(prediction_low),
        "prediction_high": round(prediction_high),
        "prediction_low_wide": round(prediction_low_wide),
        "prediction_high_wide": round(prediction_high_wide),
        "confidence": round(confidence, 2),
        "sample_count": model.sample_count,
        "model_version": model.model_version,
        "prediction_target": "log_likes_and_top_percentile_probability",
        "rank_score": round(rank_signal, 4),
        "ranking_value": round(rank_signal * 1000000),
        "r2_training": None,
        "r2_validation": model.r2_log_validation,
        "mae_validation": model.mae_validation,
        "log_mae_validation": model.log_mae_validation,
        "wape_validation": model.wape_validation,
        "spearman_validation": model.spearman_validation,
        "validation_strategy": model.validation_strategy,
        "validation_count": model.validation_count,
        "ridge_alpha": model.ridge_alpha,
        "ridge_weight": model.ridge_weight,
        "train_median_likes": model.train_median_likes,
        "train_p75_likes": model.train_p75_likes,
        "train_p90_likes": model.train_p90_likes,
        **{key: round(value, 3) for key, value in probabilities.items()},
    }


def prediction_payload(model: AdvancedPredictionModel) -> dict[str, Any]:
    data = model.__dict__.copy()
    for key in [
        "vocab_tags",
        "vocab_person",
        "vocab_company",
        "vocab_post_type",
        "vocab_hook_tokens",
        "means",
        "scales",
        "coefficients",
        "intercept",
        "post_type_log_baselines",
        "validation_predicted_logs",
        "validation_likes",
    ]:
        data.pop(key, None)
    return data


def _feature_order(
    vocab_tags: list[str],
    vocab_person: list[str],
    vocab_company: list[str],
    vocab_post_type: list[str],
    vocab_hook_tokens: list[str],
) -> list[str]:
    feature_order = list(FEATURE_ORDER)
    feature_order.extend(
        [
            "is_animated",
            "has_hook_text",
            "hook_token_count",
            "published_hour_sin",
            "published_hour_cos",
            "published_weekday_sin",
            "published_weekday_cos",
        ]
    )
    feature_order.extend([f"tag_{t}" for t in vocab_tags])
    feature_order.extend([f"person_{p}" for p in vocab_person])
    feature_order.extend([f"company_{c}" for c in vocab_company])
    feature_order.extend([f"post_type_{pt}" for pt in vocab_post_type])
    feature_order.extend([f"hook_token_{token}" for token in vocab_hook_tokens])
    return feature_order


def _select_model(
    x: np.ndarray,
    y_log: np.ndarray,
    y_likes: np.ndarray,
    rows: list[dict[str, Any]],
    train_idx: np.ndarray,
    val_idx: np.ndarray,
) -> dict[str, Any]:
    train_x = x[train_idx]
    val_x = x[val_idx]
    train_y_log = y_log[train_idx]
    val_y_log = y_log[val_idx]
    val_y_likes = y_likes[val_idx]
    train_scaled, means, scales = _standardize(train_x)
    val_scaled = np.nan_to_num((np.clip(val_x, -1e6, 1e6) - means) / scales, nan=0.0, posinf=0.0, neginf=0.0)

    train_rows = [rows[int(index)] for index in train_idx]
    val_rows = [rows[int(index)] for index in val_idx]
    train_group_baselines = _post_type_log_baselines(train_rows, train_y_log)
    train_global_baseline = float(np.median(train_y_log))
    val_baseline = np.array([
        _baseline_from_map(row, train_global_baseline, train_group_baselines)
        for row in val_rows
    ])

    best: dict[str, Any] | None = None
    for alpha in [0.05, 0.1, 0.3, 1.0, 2.5, 5.0, 10.0, 25.0, 75.0, 150.0]:
        beta = _fit_ridge(train_scaled, train_y_log, alpha=alpha)
        ridge_log = _predict_log(val_scaled, beta)
        for ridge_weight in [index / 20.0 for index in range(0, 21)]:
            predicted_log = (ridge_weight * ridge_log) + ((1.0 - ridge_weight) * val_baseline)
            predicted_likes = np.expm1(np.clip(predicted_log, 0.0, 20.0))
            metrics = _validation_metrics(val_y_likes, val_y_log, predicted_likes, predicted_log)
            # A/B decisions need ordering signal, not only exact-like accuracy.
            # Keep log error as the primary objective, but reward temporal-holdout
            # rank correlation enough to avoid collapsing to a flat post-type median.
            score = metrics["log_mae"] - (0.20 * max(metrics["spearman"] or 0.0, 0.0))
            if best is None or score < best["score"]:
                best = {
                    "score": score,
                    "alpha": alpha,
                    "ridge_weight": ridge_weight,
                    "validation_predicted_logs": predicted_log,
                    **metrics,
                }
    if best is None:
        raise ValueError("Unable to select prediction model.")
    return best


def _fit_ridge(x_scaled: np.ndarray, y_log: np.ndarray, alpha: float) -> np.ndarray:
    x_scaled = np.nan_to_num(x_scaled, nan=0.0, posinf=0.0, neginf=0.0)
    x_scaled = np.clip(x_scaled, -1e6, 1e6)
    design = np.column_stack([np.ones(x_scaled.shape[0]), x_scaled])
    penalty = np.eye(design.shape[1]) * math.sqrt(alpha)
    penalty[0, 0] = 0.0
    augmented_x = np.vstack([design, penalty])
    augmented_y = np.concatenate([y_log, np.zeros(design.shape[1])])
    beta, *_ = np.linalg.lstsq(augmented_x, augmented_y, rcond=None)
    return beta


def _predict_log(x_scaled: np.ndarray, beta: np.ndarray) -> np.ndarray:
    x_scaled = np.nan_to_num(x_scaled, nan=0.0, posinf=0.0, neginf=0.0)
    x_scaled = np.clip(x_scaled, -1e6, 1e6)
    design = np.column_stack([np.ones(x_scaled.shape[0]), x_scaled])
    predicted = np.sum(design * beta.reshape(1, -1), axis=1)
    return np.clip(predicted, 0.0, 20.0)


def _validation_metrics(
    actual_likes: np.ndarray,
    actual_log: np.ndarray,
    predicted_likes: np.ndarray,
    predicted_log: np.ndarray,
) -> dict[str, float | None]:
    mae = float(np.mean(np.abs(actual_likes - predicted_likes)))
    log_mae = float(np.mean(np.abs(actual_log - predicted_log)))
    wape = float(np.sum(np.abs(actual_likes - predicted_likes)) / max(float(np.sum(actual_likes)), 1.0))
    total = float(((actual_log - actual_log.mean()) ** 2).sum())
    residual = float(((actual_log - predicted_log) ** 2).sum())
    r2_log = 1.0 - residual / total if total > 1e-9 else None
    return {
        "mae": mae,
        "log_mae": log_mae,
        "wape": wape,
        "r2_log": r2_log,
        "spearman": _spearman(actual_likes, predicted_likes),
    }


def _spearman(actual: np.ndarray, predicted: np.ndarray) -> float | None:
    if actual.size < 2 or predicted.size < 2:
        return None
    actual_ranks = _rankdata(actual)
    predicted_ranks = _rankdata(predicted)
    actual_std = float(np.std(actual_ranks))
    predicted_std = float(np.std(predicted_ranks))
    if actual_std <= 1e-9 or predicted_std <= 1e-9:
        return None
    return float(np.corrcoef(actual_ranks, predicted_ranks)[0, 1])


def _rankdata(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.shape[0], dtype=float)
    sorted_values = values[order]
    start = 0
    while start < values.shape[0]:
        end = start + 1
        while end < values.shape[0] and sorted_values[end] == sorted_values[start]:
            end += 1
        ranks[order[start:end]] = (start + end - 1) / 2.0
        start = end
    return ranks


def _post_type_log_baselines(rows: list[dict[str, Any]], y_log: np.ndarray) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for row, log_likes in zip(rows, y_log, strict=False):
        for item in _metadata_items(row, "post_type_label"):
            values.setdefault(item, []).append(float(log_likes))
    return {
        key: float(np.median(group_values))
        for key, group_values in values.items()
        if len(group_values) >= 5
    }


def _baseline_from_map(row: dict[str, Any], global_log: float, group_baselines: dict[str, float]) -> float:
    matches = [group_baselines[item] for item in _metadata_items(row, "post_type_label") if item in group_baselines]
    return float(np.mean(matches)) if matches else global_log


def _baseline_for_post(post: dict[str, Any], model: AdvancedPredictionModel) -> float:
    return _baseline_from_map(
        post,
        float(model.global_log_baseline or 0.0),
        model.post_type_log_baselines or {},
    )


def _thresholds(y_likes: np.ndarray) -> dict[str, float]:
    return {
        "median": float(np.percentile(y_likes, 50)),
        "p75": float(np.percentile(y_likes, 75)),
        "p90": float(np.percentile(y_likes, 90)),
    }


def _probability_above(predicted_log: float, threshold_likes: float, model: AdvancedPredictionModel) -> float:
    val_logs = np.array(model.validation_predicted_logs or [], dtype=float)
    val_likes = np.array(model.validation_likes or [], dtype=float)
    if val_logs.size == 0 or val_likes.size == 0:
        priors = model.probability_priors or {}
        if threshold_likes <= float(model.train_median_likes or 0.0):
            return float(priors.get("median", 0.5))
        if threshold_likes <= float(model.train_p75_likes or 0.0):
            return float(priors.get("p75", 0.25))
        return float(priors.get("p90", 0.1))
    distances = np.abs(val_logs - predicted_log)
    bandwidth = max(float(np.std(val_logs)) * 0.35, 0.12)
    weights = np.exp(-distances / bandwidth)
    local_prob = float(np.dot(weights, val_likes >= threshold_likes) / max(float(np.sum(weights)), 1e-9))
    effective_n = float((np.sum(weights) ** 2) / max(np.sum(weights * weights), 1e-9))
    prior = float(np.mean(val_likes >= threshold_likes))
    local_weight = min(0.85, effective_n / (effective_n + 18.0))
    return max(0.0, min(1.0, (local_weight * local_prob) + ((1.0 - local_weight) * prior)))


def _percentile_from_prediction(predicted_likes: float, model: AdvancedPredictionModel) -> float:
    anchors = [
        (0.0, 0.0),
        (float(model.train_median_likes or 0.0), 0.50),
        (float(model.train_p75_likes or 0.0), 0.75),
        (float(model.train_p90_likes or 0.0), 0.90),
        (float(model.train_p95_likes or 0.0), 0.95),
        (float(model.train_p99_likes or 0.0), 0.99),
        (float(model.train_max_likes or 0.0), 1.0),
    ]
    anchors = sorted({likes: pct for likes, pct in anchors}.items())
    for index in range(1, len(anchors)):
        left_likes, left_pct = anchors[index - 1]
        right_likes, right_pct = anchors[index]
        if predicted_likes <= right_likes:
            span = max(right_likes - left_likes, 1.0)
            return left_pct + ((predicted_likes - left_likes) / span) * (right_pct - left_pct)
    return 1.0
