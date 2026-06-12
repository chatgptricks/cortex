from __future__ import annotations

import math
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from .config import MIN_CALIBRATION_SAMPLES


BASE_FEATURES = [
    "global_mean_abs",
    "global_peak_abs",
    "global_std",
    "temporal_variability",
    "late_minus_early",
    "sustained_ratio",
    "left_right_balance",
]

NETWORK_FEATURES = [
    "visual",
    "attention",
    "language",
    "social",
    "memory_scene",
    "control",
    "motor",
    "valuation",
]

FEATURE_ORDER = BASE_FEATURES + [f"network_{name}" for name in NETWORK_FEATURES]
FLOP_LIKES_BASELINE = 850
HOOK_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


@dataclass
class CalibrationModel:
    ready: bool
    sample_count: int
    feature_order: list[str]
    vocab_tags: list[str] | None = None
    vocab_person: list[str] | None = None
    vocab_company: list[str] | None = None
    vocab_post_type: list[str] | None = None
    vocab_hook_tokens: list[str] | None = None
    means: list[float] | None = None
    scales: list[float] | None = None
    coefficients: list[float] | None = None
    intercept: float | None = None
    blend_alpha: float | None = None
    knn_k: int | None = None
    train_vectors_scaled: list[list[float]] | None = None
    train_likes: list[float] | None = None
    r2_training: float | None = None
    r2_validation: float | None = None
    mae_training: float | None = None
    mae_validation: float | None = None
    wape_validation: float | None = None
    validation_strategy: str | None = None
    prediction_interval_ratio: float | None = None
    prediction_interval_likes: float | None = None
    train_p25_likes: float | None = None
    train_p75_likes: float | None = None
    target_transform: str | None = None
    train_p95_likes: float | None = None
    train_median_likes: float | None = None
    message: str | None = None


def feature_vector(post: dict[str, Any], model: CalibrationModel | None = None) -> list[float]:
    summary = post.get("analysis_summary")
    if not summary:
        return [0.0] * (len(model.feature_order) if model else len(FEATURE_ORDER))
    metrics = summary.get("metrics") or {}
    networks = summary.get("networks") or {}
    values: list[float] = []
    
    for key in BASE_FEATURES:
        values.append(float(metrics.get(key) or 0.0))
    for key in NETWORK_FEATURES:
        network = networks.get(key) or {}
        values.append(float(network.get("raw") or 0.0))
        
    values.append(1.0 if post.get("is_animated") else 0.0)
    values.append(1.0 if post.get("hook_text") else 0.0)
    values.append(float(len(_hook_tokens(post.get("hook_text")))))
    values.append(_published_hour_sin(post))
    values.append(_published_hour_cos(post))
    values.append(_published_weekday_sin(post))
    values.append(_published_weekday_cos(post))
    
    if model and model.vocab_tags is not None:
        post_tags = set(_metadata_items(post, "tags"))
        for tag in model.vocab_tags:
            values.append(1.0 if tag in post_tags else 0.0)
            
    if model and model.vocab_person is not None:
        people = set(_metadata_items(post, "person_label"))
        for p in model.vocab_person:
            values.append(1.0 if p in people else 0.0)

    if model and model.vocab_company is not None:
        companies = set(_metadata_items(post, "company_label"))
        for c in model.vocab_company:
            values.append(1.0 if c in companies else 0.0)

    if model and model.vocab_post_type is not None:
        post_types = set(_metadata_items(post, "post_type_label"))
        for pt in model.vocab_post_type:
            values.append(1.0 if pt in post_types else 0.0)

    if model and model.vocab_hook_tokens is not None:
        tokens = _hook_tokens(post.get("hook_text"))
        for token in model.vocab_hook_tokens:
            values.append(1.0 if token in tokens else 0.0)
            
    return values


def _metadata_items(post: dict[str, Any], key: str) -> list[str]:
    value = post.get(key)
    if not value:
        return []
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
            raw_items = parsed if isinstance(parsed, list) else [value]
        except ValueError:
            raw_items = value.split(",")
    else:
        raw_items = [value]
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _build_vocab(posts: list[dict[str, Any]], key: str, min_freq: int = 2) -> list[str]:
    counts: dict[str, int] = {}
    for post in posts:
        for item in _metadata_items(post, key):
            counts[item] = counts.get(item, 0) + 1
    return sorted([k for k, v in counts.items() if v >= min_freq])


def _build_hook_vocab(posts: list[dict[str, Any]], min_freq: int = 4, limit: int = 120) -> list[str]:
    counts: dict[str, int] = {}
    for post in posts:
        for token in _hook_tokens(post.get("hook_text")):
            counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, count in ranked if count >= min_freq][:limit]


def _training_likes(post: dict[str, Any]) -> int:
    likes = post.get("likes")
    if likes is None:
        return FLOP_LIKES_BASELINE
    return max(0, int(likes))


def _hook_tokens(text: Any) -> set[str]:
    if not text:
        return set()
    normalized = str(text).lower()
    tokens = [token for token in HOOK_TOKEN_RE.findall(normalized) if len(token) >= 3]
    return set(tokens)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _cyclic(value: int, period: int, fn: str) -> float:
    angle = 2.0 * math.pi * (value % period) / period
    return math.sin(angle) if fn == "sin" else math.cos(angle)


def _published_hour_sin(post: dict[str, Any]) -> float:
    parsed = _parse_datetime(post.get("published_at"))
    return _cyclic(parsed.hour, 24, "sin") if parsed else 0.0


def _published_hour_cos(post: dict[str, Any]) -> float:
    parsed = _parse_datetime(post.get("published_at"))
    return _cyclic(parsed.hour, 24, "cos") if parsed else 0.0


def _published_weekday_sin(post: dict[str, Any]) -> float:
    parsed = _parse_datetime(post.get("published_at"))
    return _cyclic(parsed.weekday(), 7, "sin") if parsed else 0.0


def _published_weekday_cos(post: dict[str, Any]) -> float:
    parsed = _parse_datetime(post.get("published_at"))
    return _cyclic(parsed.weekday(), 7, "cos") if parsed else 0.0


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if left_norm <= 1e-9 or right_norm <= 1e-9:
        return 0.0
    similarity = float(np.dot(left, right) / (left_norm * right_norm))
    return max(0.0, min(1.0, similarity))


def _knn_predict(
    target_scaled: np.ndarray,
    train_scaled: np.ndarray,
    train_likes: np.ndarray,
    k: int,
) -> float:
    if train_scaled.size == 0 or train_likes.size == 0:
        return 0.0
    k = max(3, min(k, train_likes.shape[0]))
    distances = np.linalg.norm(train_scaled - target_scaled, axis=1)
    nearest = np.argpartition(distances, k - 1)[:k]
    nearest_distances = distances[nearest]
    nearest_likes = train_likes[nearest]
    weights = 1.0 / np.maximum(nearest_distances, 1e-6)
    return float(np.dot(weights, nearest_likes) / np.sum(weights))


def _standardize(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x = np.clip(x, -1e6, 1e6)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales < 1e-9] = 1.0
    return (x - means) / scales, means, scales


def _fit_ridge(x_scaled: np.ndarray, y_log: np.ndarray, alpha: float = 2.5) -> np.ndarray:
    x_scaled = np.nan_to_num(x_scaled, nan=0.0, posinf=0.0, neginf=0.0)
    x_scaled = np.clip(x_scaled, -1e6, 1e6)
    design = np.column_stack([np.ones(x_scaled.shape[0]), x_scaled])
    penalty = np.eye(design.shape[1]) * math.sqrt(alpha)
    penalty[0, 0] = 0.0
    augmented_x = np.vstack([design, penalty])
    augmented_y = np.concatenate([y_log, np.zeros(design.shape[1])])
    beta, *_ = np.linalg.lstsq(augmented_x, augmented_y, rcond=None)
    return beta


def _predict_ridge_raw(x_scaled: np.ndarray, beta: np.ndarray) -> np.ndarray:
    x_scaled = np.nan_to_num(x_scaled, nan=0.0, posinf=0.0, neginf=0.0)
    x_scaled = np.clip(x_scaled, -1e6, 1e6)
    design = np.column_stack([np.ones(x_scaled.shape[0]), x_scaled])
    predicted_log = np.sum(design * beta.reshape(1, -1), axis=1)
    return np.maximum(0.0, np.expm1(np.clip(predicted_log, -20.0, 20.0)))


def _validation_indices(training_rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, str]:
    sample_count = len(training_rows)
    validation_count = max(80, int(sample_count * 0.2))
    validation_count = min(validation_count, sample_count - 1)

    dated = [
        (index, _parse_datetime(row.get("published_at")))
        for index, row in enumerate(training_rows)
    ]
    if sum(1 for _, value in dated if value is not None) >= max(100, int(sample_count * 0.65)):
        ordered = np.array([
            index
            for index, _ in sorted(
                dated,
                key=lambda item: item[1].timestamp() if item[1] else float("-inf"),
            )
        ])
        return ordered[:-validation_count], ordered[-validation_count:], "time_holdout_newest_posts"

    rng = np.random.default_rng(42)
    indices = np.arange(sample_count)
    rng.shuffle(indices)
    return indices[:-validation_count], indices[-validation_count:], "random_holdout_seed_42"


def _weighted_absolute_percentage_error(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.sum(np.abs(actual - predicted)) / max(float(np.sum(np.abs(actual))), 1.0))


def fit_calibration(posts: list[dict[str, Any]]) -> CalibrationModel:
    training_rows = [
        post
        for post in posts
        if post.get("section") == "historical"
        and post.get("status") == "completed"
        and post.get("analysis_summary")
    ]
    sample_count = len(training_rows)
    if sample_count < MIN_CALIBRATION_SAMPLES:
        missing = MIN_CALIBRATION_SAMPLES - sample_count
        return CalibrationModel(
            ready=False,
            sample_count=sample_count,
            feature_order=FEATURE_ORDER,
            message=f"{missing} analyzed Post DB posts still needed.",
        )

    vocab_tags = _build_vocab(training_rows, "tags")
    vocab_person = _build_vocab(training_rows, "person_label")
    vocab_company = _build_vocab(training_rows, "company_label")
    vocab_post_type = _build_vocab(training_rows, "post_type_label")
    vocab_hook_tokens = _build_hook_vocab(training_rows)

    feature_order = list(FEATURE_ORDER)
    feature_order.append("is_animated")
    feature_order.append("has_hook_text")
    feature_order.append("hook_token_count")
    feature_order.extend([
        "published_hour_sin",
        "published_hour_cos",
        "published_weekday_sin",
        "published_weekday_cos",
    ])
    feature_order.extend([f"tag_{t}" for t in vocab_tags])
    feature_order.extend([f"person_{p}" for p in vocab_person])
    feature_order.extend([f"company_{c}" for c in vocab_company])
    feature_order.extend([f"post_type_{pt}" for pt in vocab_post_type])
    feature_order.extend([f"hook_token_{token}" for token in vocab_hook_tokens])

    model_stub = CalibrationModel(
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
        np.array([feature_vector(row, model_stub) for row in training_rows]),
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    )
    y_raw = np.array([float(_training_likes(row)) for row in training_rows])
    y = np.log1p(y_raw)
    x_scaled, means, scales = _standardize(x)
    beta = _fit_ridge(x_scaled, y)
    preds_raw = _predict_ridge_raw(x_scaled, beta)
    total = float(((y_raw - y_raw.mean()) ** 2).sum())
    residual = float(((y_raw - preds_raw) ** 2).sum())
    r2 = 1.0 - residual / total if total > 1e-9 else None
    mae_training = float(np.mean(np.abs(y_raw - preds_raw)))

    # Validation-tuned blend: linear ridge + KNN over historical feature space.
    train_idx, val_idx, validation_strategy = _validation_indices(training_rows)
    train_x = x[train_idx]
    val_x = x[val_idx]
    train_y_log = y[train_idx]
    train_likes = y_raw[train_idx]
    val_likes = y_raw[val_idx]
    train_scaled, validation_means, validation_scales = _standardize(train_x)
    val_scaled = (val_x - validation_means) / validation_scales
    validation_beta = _fit_ridge(train_scaled, train_y_log)
    ridge_val_pred = _predict_ridge_raw(val_scaled, validation_beta)

    knn_k_candidates = [10, 15, 20, 25, 35]
    alpha_candidates = [i / 20.0 for i in range(0, 21)]
    best_mae = float("inf")
    best_wape = float("inf")
    best_r2 = None
    best_alpha = 0.8
    best_k = 20
    best_blended_val = ridge_val_pred

    for k in knn_k_candidates:
        knn_val_pred = np.array([
            _knn_predict(vector, train_scaled, train_likes, k)
            for vector in val_scaled
        ])
        for alpha in alpha_candidates:
            blended = (alpha * ridge_val_pred) + ((1.0 - alpha) * knn_val_pred)
            mae = float(np.mean(np.abs(val_likes - blended)))
            if mae < best_mae:
                val_total = float(((val_likes - val_likes.mean()) ** 2).sum())
                val_residual = float(((val_likes - blended) ** 2).sum())
                best_mae = mae
                best_wape = _weighted_absolute_percentage_error(val_likes, blended)
                best_r2 = 1.0 - val_residual / val_total if val_total > 1e-9 else None
                best_alpha = alpha
                best_k = k
                best_blended_val = blended

    relative_errors = np.abs(val_likes - best_blended_val) / np.maximum(val_likes, 1.0)
    interval_ratio = float(np.percentile(relative_errors, 75))

    model_stub.ready = True
    model_stub.means = means.tolist()
    model_stub.scales = scales.tolist()
    model_stub.intercept = float(beta[0])
    model_stub.coefficients = beta[1:].tolist()
    model_stub.blend_alpha = float(best_alpha)
    model_stub.knn_k = int(best_k)
    # Keep as numpy arrays: list-of-lists of Python floats costs ~10x the RAM.
    model_stub.train_vectors_scaled = x_scaled  # type: ignore[assignment]
    model_stub.train_likes = y_raw  # type: ignore[assignment]
    model_stub.r2_training = r2
    model_stub.r2_validation = best_r2
    model_stub.mae_training = mae_training
    model_stub.mae_validation = best_mae
    model_stub.wape_validation = best_wape
    model_stub.validation_strategy = validation_strategy
    model_stub.prediction_interval_ratio = interval_ratio
    model_stub.prediction_interval_likes = best_mae
    model_stub.target_transform = "log1p_likes"
    model_stub.train_p25_likes = float(np.percentile(y_raw, 25))
    model_stub.train_p95_likes = float(np.percentile(y_raw, 95))
    model_stub.train_p75_likes = float(np.percentile(y_raw, 75))
    model_stub.train_median_likes = float(np.median(y_raw))
    model_stub.message = "Prediction model trained on Post DB: TRIBE v2 features + metadata + hook text, using ridge + KNN tuned on holdout validation."
    
    return model_stub


def predict_likes(
    post: dict[str, Any],
    model: CalibrationModel,
    all_posts: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not model.ready or not post.get("analysis_summary"):
        return None
    x = np.array(feature_vector(post, model), dtype=float)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x = np.clip(x, -1e6, 1e6)
    means = np.array(model.means, dtype=float)
    scales = np.array(model.scales, dtype=float)
    coefficients = np.array(model.coefficients, dtype=float)
    predicted_log = float(model.intercept + ((x - means) / scales) @ coefficients)
    ridge_predicted = max(0.0, math.expm1(predicted_log))
    x_scaled = (x - means) / scales
    if model.train_vectors_scaled and model.train_likes and model.knn_k:
        train_scaled = np.array(model.train_vectors_scaled, dtype=float)
        train_likes = np.array(model.train_likes, dtype=float)
        knn_predicted = max(0.0, _knn_predict(x_scaled, train_scaled, train_likes, int(model.knn_k)))
    else:
        knn_predicted = ridge_predicted
    alpha = float(model.blend_alpha if model.blend_alpha is not None else 0.8)
    model_predicted = (alpha * ridge_predicted) + ((1.0 - alpha) * knn_predicted)
    baseline = float(model.train_median_likes or 0.0)
    # Blend toward historical baseline when sample size is still limited.
    model_weight = min(0.9, max(0.35, model.sample_count / (model.sample_count + 35.0)))
    predicted = (model_weight * model_predicted) + ((1.0 - model_weight) * baseline)
    if model.train_p95_likes is not None:
        # Conservative upper guardrail to reduce exaggerated long-tail estimates.
        tail_multiplier = 1.35 if model.sample_count < 80 else 1.6
        predicted = min(predicted, float(model.train_p95_likes) * tail_multiplier)
    predicted = max(0.0, predicted)

    similarity_signal: dict[str, Any] | None = None
    if all_posts:
        historical_rows = [
            row
            for row in all_posts
            if row.get("section") == "historical"
            and row.get("status") == "completed"
            and row.get("analysis_summary")
            and row.get("likes") is not None
        ]
        if historical_rows:
            target_vector = np.array(feature_vector(post, model), dtype=float)
            target_hook_tokens = _hook_tokens(post.get("hook_text"))
            target_elements = (
                set(_metadata_items(post, "tags"))
                | set(_metadata_items(post, "person_label"))
                | set(_metadata_items(post, "company_label"))
                | set(_metadata_items(post, "post_type_label"))
            )

            brain_dims = len(BASE_FEATURES) + len(NETWORK_FEATURES) + 2
            target_brain = target_vector[:brain_dims]
            candidates: list[tuple[float, float]] = []
            weighted_similarity = 0.0
            successful_votes = 0
            average_votes = 0
            p75_likes = float(np.percentile([float(_training_likes(row)) for row in historical_rows], 75))
            p40_likes = float(np.percentile([float(_training_likes(row)) for row in historical_rows], 40))

            for row in historical_rows:
                row_vector = np.array(feature_vector(row, model), dtype=float)
                row_brain = row_vector[:brain_dims]
                brain_similarity = _cosine_similarity(target_brain, row_brain)
                hook_similarity = _jaccard(target_hook_tokens, _hook_tokens(row.get("hook_text")))
                row_elements = (
                    set(_metadata_items(row, "tags"))
                    | set(_metadata_items(row, "person_label"))
                    | set(_metadata_items(row, "company_label"))
                    | set(_metadata_items(row, "post_type_label"))
                )
                element_similarity = _jaccard(target_elements, row_elements)
                similarity = (0.55 * brain_similarity) + (0.3 * hook_similarity) + (0.15 * element_similarity)
                if similarity < 0.2:
                    continue
                row_likes = float(_training_likes(row))
                candidates.append((similarity, row_likes))
                weighted_similarity += similarity
                if row_likes >= p75_likes:
                    successful_votes += 1
                elif row_likes <= p40_likes:
                    average_votes += 1

            if candidates:
                top_neighbors = sorted(candidates, key=lambda item: item[0], reverse=True)[:7]
                top_similarity = float(np.mean([value[0] for value in top_neighbors]))
                total_neighbor_weight = sum(value[0] for value in top_neighbors)
                neighbor_likes = (
                    sum(sim * likes for sim, likes in top_neighbors) / max(total_neighbor_weight, 1e-9)
                )
                similarity_blend = min(0.45, max(0.0, (top_similarity - 0.2) * 0.7))
                predicted = ((1.0 - similarity_blend) * predicted) + (similarity_blend * neighbor_likes)
                cluster = "successful" if successful_votes > average_votes else "average"
                similarity_signal = {
                    "cluster": cluster,
                    "neighbor_avg_likes": round(neighbor_likes),
                    "top_similarity": round(top_similarity, 3),
                    "neighbors": len(top_neighbors),
                }

    sample_confidence = min(0.95, max(0.2, math.log10(model.sample_count + 1) / 3.5))
    error_confidence = 1.0 / (1.0 + max(0.0, float(model.wape_validation or 0.0)))
    confidence = min(0.9, max(0.2, sample_confidence * error_confidence))
    interval_likes = float(model.prediction_interval_likes or model.mae_validation or (predicted * 0.35))
    prediction_low = max(0.0, predicted - interval_likes)
    prediction_high = predicted + interval_likes
    response = {
        "predicted_likes": round(predicted),
        "prediction_low": round(prediction_low),
        "prediction_high": round(prediction_high),
        "confidence": round(confidence, 2),
        "sample_count": model.sample_count,
        "r2_training": model.r2_training,
        "r2_validation": model.r2_validation,
        "mae_training": model.mae_training,
        "mae_validation": model.mae_validation,
        "wape_validation": model.wape_validation,
        "validation_strategy": model.validation_strategy,
        "blend_alpha": model.blend_alpha,
        "knn_k": model.knn_k,
    }
    if similarity_signal:
        response["similarity_signal"] = similarity_signal
    return response
