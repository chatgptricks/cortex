from __future__ import annotations

import math
import os
import re
import zlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .calibration import (
    BASE_FEATURES,
    NETWORK_FEATURES,
    _build_vocab,
    _cyclic,
    _metadata_items,
    _parse_datetime,
)
from .prediction_model import _spearman

MODEL_VERSION = "multi_signal_v2"
MIN_V2_SAMPLES = 30
ROLLING_WINDOW = 20
HASH_DIMS = 32
TOKEN_RE = re.compile(r"[#@]?[a-z0-9áéíóúñü]+", re.IGNORECASE)
CTA_WORDS = {
    "link", "bio", "comment", "comenta", "comments", "share", "comparte",
    "save", "guarda", "follow", "sigue", "dm", "tag", "etiqueta",
}
PROBABILITY_KEYS = {
    "median": "probability_above_median",
    "p75": "probability_above_p75",
    "p90": "probability_above_p90",
}


@dataclass
class MultiSignalModel:
    ready: bool
    sample_count: int
    feature_order: list[str] = field(default_factory=list)
    model_version: str = MODEL_VERSION
    message: str | None = None
    # vocabularies
    vocab_tags: list[str] | None = None
    vocab_person: list[str] | None = None
    vocab_company: list[str] | None = None
    vocab_post_type: list[str] | None = None
    # ridge parameters
    means: np.ndarray | None = None
    scales: np.ndarray | None = None
    ridge_beta: np.ndarray | None = None
    ridge_alpha: float | None = None
    # gbm (in-memory only; not serialized in payload)
    gbm: Any | None = None
    gbm_weight: float = 0.0
    # account baseline at predict time
    current_baseline_log: float | None = None
    current_baseline_mean_log: float | None = None
    last_published_at: str | None = None
    # OOF calibration artifacts
    oof_predicted_logs: list[float] | None = None
    oof_actual_likes: list[float] | None = None
    interval_log_q80: float | None = None
    interval_log_q90: float | None = None
    # training distribution
    train_median_likes: float | None = None
    train_p75_likes: float | None = None
    train_p90_likes: float | None = None
    train_p95_likes: float | None = None
    train_p99_likes: float | None = None
    train_max_likes: float | None = None
    probability_priors: dict[str, float] | None = None
    # metrics
    validation_strategy: str | None = None
    validation_count: int | None = None
    mae_validation: float | None = None
    log_mae_validation: float | None = None
    wape_validation: float | None = None
    r2_log_validation: float | None = None
    spearman_validation: float | None = None
    used_gbm: bool = False


# ---------------------------------------------------------------------------
# Text feature helpers
# ---------------------------------------------------------------------------

def _tokens(text: Any) -> list[str]:
    if not text:
        return []
    return [token.lower() for token in TOKEN_RE.findall(str(text)) if len(token) >= 2]


def _hashed_counts(tokens: list[str], dims: int = HASH_DIMS) -> list[float]:
    counts = [0.0] * dims
    for token in tokens:
        counts[zlib.crc32(token.encode("utf-8")) % dims] += 1.0
    return counts


def _emoji_count(text: str) -> int:
    return sum(1 for ch in text if ord(ch) >= 0x1F000 or 0x2600 <= ord(ch) <= 0x27BF)


def _hook_features(post: dict[str, Any]) -> list[float]:
    text = str(post.get("hook_text") or "")
    tokens = _tokens(text)
    letters = [ch for ch in text if ch.isalpha()]
    caps_ratio = (sum(1 for ch in letters if ch.isupper()) / len(letters)) if letters else 0.0
    return [
        float(len(text)),
        float(len(tokens)),
        1.0 if any(ch.isdigit() for ch in text) else 0.0,
        1.0 if "?" in text else 0.0,
        1.0 if "!" in text else 0.0,
        caps_ratio,
        *_hashed_counts(tokens),
    ]


def _caption_features(post: dict[str, Any]) -> list[float]:
    text = str(post.get("caption") or "")
    tokens = _tokens(text)
    return [
        float(len(text)),
        float(len(tokens)),
        float(text.count("\n") + 1 if text else 0),
        float(sum(1 for t in tokens if t.startswith("#"))),
        float(sum(1 for t in tokens if t.startswith("@"))),
        float(_emoji_count(text)),
        1.0 if "?" in text else 0.0,
        float(sum(1 for t in tokens if t.lstrip("#@") in CTA_WORDS)),
        *_hashed_counts(tokens),
    ]


# ---------------------------------------------------------------------------
# Brain feature helpers
# ---------------------------------------------------------------------------

def _brain_features(post: dict[str, Any]) -> list[float]:
    summary = post.get("analysis_summary") or {}
    metrics = summary.get("metrics") or {}
    networks = summary.get("networks") or {}
    values: list[float] = [float(metrics.get(key) or 0.0) for key in BASE_FEATURES]
    for key in NETWORK_FEATURES:
        network = networks.get(key) or {}
        values.append(float(network.get("raw") or 0.0))
        values.append(float(network.get("score") or 0.0) / 100.0)
    regions = summary.get("top_regions") or []
    raws = [float(region.get("raw") or 0.0) for region in regions[:5]]
    values.append(float(np.mean(raws)) if raws else 0.0)
    values.append(float(np.max(raws)) if raws else 0.0)
    values.append(float(sum(1 for region in regions if float(region.get("score") or 0.0) >= 50.0)))
    values.append(float(summary.get("virality_potential") or post.get("virality_potential") or 0.0))
    return values


def _brain_feature_names() -> list[str]:
    names = list(BASE_FEATURES)
    for key in NETWORK_FEATURES:
        names.extend([f"network_{key}_raw", f"network_{key}_score"])
    names.extend(["top5_region_mean", "top5_region_max", "regions_above_50", "virality_potential"])
    return names


# ---------------------------------------------------------------------------
# Time / metadata helpers
# ---------------------------------------------------------------------------

def _time_features(post: dict[str, Any]) -> list[float]:
    parsed = _parse_datetime(post.get("published_at"))
    if not parsed:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        _cyclic(parsed.hour, 24, "sin"),
        _cyclic(parsed.hour, 24, "cos"),
        _cyclic(parsed.weekday(), 7, "sin"),
        _cyclic(parsed.weekday(), 7, "cos"),
    ]


def _vocab_features(post: dict[str, Any], key: str, vocab: list[str] | None) -> list[float]:
    if not vocab:
        return []
    items = set(_metadata_items(post, key))
    return [1.0 if entry in items else 0.0 for entry in vocab]


def _sort_key(post: dict[str, Any]) -> float:
    parsed = _parse_datetime(post.get("published_at")) or _parse_datetime(post.get("created_at"))
    return parsed.timestamp() if parsed else float("-inf")


def _rolling_baselines(y_log: np.ndarray, window: int = ROLLING_WINDOW) -> tuple[np.ndarray, np.ndarray]:
    """Per-row median/mean of the previous `window` log-likes (time-ordered)."""
    n = y_log.shape[0]
    medians = np.zeros(n)
    means = np.zeros(n)
    global_median = float(np.median(y_log))
    for i in range(n):
        previous = y_log[max(0, i - window): i]
        if previous.size >= 5:
            medians[i] = float(np.median(previous))
            means[i] = float(np.mean(previous))
        else:
            medians[i] = global_median
            means[i] = global_median
    return medians, means


def _days_since_previous(rows: list[dict[str, Any]]) -> np.ndarray:
    stamps = [_sort_key(row) for row in rows]
    deltas = np.zeros(len(rows))
    for i in range(1, len(rows)):
        if math.isfinite(stamps[i]) and math.isfinite(stamps[i - 1]):
            deltas[i] = min(30.0, max(0.0, (stamps[i] - stamps[i - 1]) / 86400.0))
    return deltas


# ---------------------------------------------------------------------------
# Feature assembly
# ---------------------------------------------------------------------------

def _feature_vector(
    post: dict[str, Any],
    model: MultiSignalModel,
    baseline_median: float,
    baseline_mean: float,
    days_since_prev: float,
) -> list[float]:
    values: list[float] = []
    values.extend(_brain_features(post))
    values.extend(_hook_features(post))
    values.extend(_caption_features(post))
    values.append(1.0 if post.get("is_animated") else 0.0)
    values.extend(_time_features(post))
    values.extend(_vocab_features(post, "tags", model.vocab_tags))
    values.extend(_vocab_features(post, "person_label", model.vocab_person))
    values.extend(_vocab_features(post, "company_label", model.vocab_company))
    values.extend(_vocab_features(post, "post_type_label", model.vocab_post_type))
    values.extend([baseline_median, baseline_mean, days_since_prev])
    return values


def _feature_order(model: MultiSignalModel) -> list[str]:
    names = _brain_feature_names()
    names.extend(
        ["hook_chars", "hook_tokens", "hook_has_digits", "hook_question", "hook_exclaim", "hook_caps_ratio"]
        + [f"hook_hash_{i}" for i in range(HASH_DIMS)]
    )
    names.extend(
        [
            "caption_chars", "caption_tokens", "caption_lines", "caption_hashtags",
            "caption_mentions", "caption_emojis", "caption_question", "caption_cta",
        ]
        + [f"caption_hash_{i}" for i in range(HASH_DIMS)]
    )
    names.append("is_animated")
    names.extend(["published_hour_sin", "published_hour_cos", "published_weekday_sin", "published_weekday_cos"])
    names.extend([f"tag_{t}" for t in (model.vocab_tags or [])])
    names.extend([f"person_{p}" for p in (model.vocab_person or [])])
    names.extend([f"company_{c}" for c in (model.vocab_company or [])])
    names.extend([f"post_type_{pt}" for pt in (model.vocab_post_type or [])])
    names.extend(["rolling_median_log", "rolling_mean_log", "days_since_previous_post"])
    return names


# ---------------------------------------------------------------------------
# Linear algebra
# ---------------------------------------------------------------------------

def _standardize(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x = np.clip(x, -1e6, 1e6)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales < 1e-9] = 1.0
    return (x - means) / scales, means, scales


def _fit_ridge(x_scaled: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    design = np.column_stack([np.ones(x_scaled.shape[0]), x_scaled])
    penalty = np.eye(design.shape[1]) * math.sqrt(alpha)
    penalty[0, 0] = 0.0
    augmented_x = np.vstack([design, penalty])
    augmented_y = np.concatenate([y, np.zeros(design.shape[1])])
    beta, *_ = np.linalg.lstsq(augmented_x, augmented_y, rcond=None)
    return beta


def _predict_ridge(x: np.ndarray, means: np.ndarray, scales: np.ndarray, beta: np.ndarray) -> np.ndarray:
    x_scaled = np.nan_to_num((np.clip(x, -1e6, 1e6) - means) / scales, nan=0.0, posinf=0.0, neginf=0.0)
    design = np.column_stack([np.ones(x_scaled.shape[0]), x_scaled])
    return design @ beta


def _make_gbm() -> Any | None:
    # Opt-in only: sklearn's OpenMP-backed GBM can hang inside server threads on
    # some macOS setups, and it adds <1% over ridge on current data. Set
    # PREDICT_V2_GBM=1 to enable.
    if os.getenv("PREDICT_V2_GBM", "").strip() != "1":
        return None
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor
    except ImportError:
        return None
    return HistGradientBoostingRegressor(
        max_iter=250,
        learning_rate=0.05,
        max_depth=3,
        min_samples_leaf=5,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=42,
    )


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def _expanding_folds(n: int, max_folds: int = 4, min_train: int = 15) -> list[tuple[np.ndarray, np.ndarray]]:
    """Contiguous expanding-window folds over the newest ~40% of rows."""
    validation_size = max(8, int(n * 0.4))
    validation_size = min(validation_size, n - min_train)
    if validation_size <= 0:
        return []
    fold_count = min(max_folds, max(1, validation_size // 8))
    bounds = np.linspace(n - validation_size, n, fold_count + 1, dtype=int)
    folds = []
    for i in range(fold_count):
        start, end = int(bounds[i]), int(bounds[i + 1])
        if end > start:
            folds.append((np.arange(0, start), np.arange(start, end)))
    return folds


def fit_multi_signal(posts: list[dict[str, Any]]) -> MultiSignalModel:
    rows = [
        post
        for post in posts
        if post.get("section") == "historical"
        and post.get("status") == "completed"
        and post.get("analysis_summary")
        and post.get("likes") is not None
    ]
    rows.sort(key=_sort_key)
    sample_count = len(rows)
    if sample_count < MIN_V2_SAMPLES:
        return MultiSignalModel(
            ready=False,
            sample_count=sample_count,
            message=f"multi_signal_v2 needs {MIN_V2_SAMPLES} analyzed historical posts with likes; {sample_count} available.",
        )

    model = MultiSignalModel(
        ready=False,
        sample_count=sample_count,
        vocab_tags=_build_vocab(rows, "tags"),
        vocab_person=_build_vocab(rows, "person_label"),
        vocab_company=_build_vocab(rows, "company_label"),
        vocab_post_type=_build_vocab(rows, "post_type_label"),
    )
    model.feature_order = _feature_order(model)

    y_likes = np.array([max(0.0, float(post.get("likes") or 0)) for post in rows])
    y_log = np.log1p(y_likes)
    baseline_median, baseline_mean = _rolling_baselines(y_log)
    days_prev = _days_since_previous(rows)
    x = np.array(
        [
            _feature_vector(post, model, baseline_median[i], baseline_mean[i], days_prev[i])
            for i, post in enumerate(rows)
        ],
        dtype=float,
    )
    x = np.clip(np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0), -1e6, 1e6)
    # De-trended target: lift over the account's rolling baseline.
    y_residual = y_log - baseline_median

    folds = _expanding_folds(sample_count)
    if not folds:
        return MultiSignalModel(
            ready=False,
            sample_count=sample_count,
            message="multi_signal_v2 could not build expanding-window folds.",
        )

    alphas = [0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0]
    oof_ridge = {alpha: {} for alpha in alphas}
    oof_gbm: dict[int, float] = {}
    gbm_available = _make_gbm() is not None

    for train_idx, val_idx in folds:
        train_scaled, fold_means, fold_scales = _standardize(x[train_idx])
        val_pred_input = x[val_idx]
        for alpha in alphas:
            beta = _fit_ridge(train_scaled, y_residual[train_idx], alpha)
            preds = _predict_ridge(val_pred_input, fold_means, fold_scales, beta)
            for j, idx in enumerate(val_idx):
                oof_ridge[alpha][int(idx)] = float(preds[j])
        if gbm_available:
            try:
                gbm = _make_gbm()
                gbm.fit(x[train_idx], y_residual[train_idx])
                preds = gbm.predict(val_pred_input)
                for j, idx in enumerate(val_idx):
                    oof_gbm[int(idx)] = float(preds[j])
            except Exception:
                gbm_available = False
                oof_gbm = {}

    oof_indices = sorted(oof_ridge[alphas[0]].keys())
    oof_idx_arr = np.array(oof_indices, dtype=int)
    actual_log = y_log[oof_idx_arr]
    actual_likes = y_likes[oof_idx_arr]
    base_for_oof = baseline_median[oof_idx_arr]

    def _score(pred_residual: np.ndarray) -> tuple[float, dict[str, float | None]]:
        predicted_log = np.clip(base_for_oof + pred_residual, 0.0, 20.0)
        predicted_likes = np.expm1(predicted_log)
        log_mae = float(np.mean(np.abs(actual_log - predicted_log)))
        mae = float(np.mean(np.abs(actual_likes - predicted_likes)))
        wape = float(np.sum(np.abs(actual_likes - predicted_likes)) / max(float(np.sum(actual_likes)), 1.0))
        total = float(((actual_log - actual_log.mean()) ** 2).sum())
        residual_ss = float(((actual_log - predicted_log) ** 2).sum())
        r2_log = 1.0 - residual_ss / total if total > 1e-9 else None
        spearman = _spearman(actual_likes, predicted_likes)
        score = log_mae - 0.20 * max(spearman or 0.0, 0.0)
        return score, {
            "mae": mae, "log_mae": log_mae, "wape": wape,
            "r2_log": r2_log, "spearman": spearman,
        }

    best_alpha, best_alpha_score = alphas[0], float("inf")
    ridge_oof_by_alpha: dict[float, np.ndarray] = {}
    for alpha in alphas:
        preds = np.array([oof_ridge[alpha][i] for i in oof_indices])
        ridge_oof_by_alpha[alpha] = preds
        score, _metrics = _score(preds)
        if score < best_alpha_score:
            best_alpha_score, best_alpha = score, alpha
    ridge_oof = ridge_oof_by_alpha[best_alpha]

    gbm_weight = 0.0
    ensemble_oof = ridge_oof
    if gbm_available and len(oof_gbm) == len(oof_indices):
        gbm_oof = np.array([oof_gbm[i] for i in oof_indices])
        best_combo_score = best_alpha_score
        for w in [i / 10.0 for i in range(0, 11)]:
            blended = (w * gbm_oof) + ((1.0 - w) * ridge_oof)
            score, _metrics = _score(blended)
            if score < best_combo_score:
                best_combo_score = score
                gbm_weight = w
                ensemble_oof = blended

    _final_score, metrics = _score(ensemble_oof)
    oof_predicted_log = np.clip(base_for_oof + ensemble_oof, 0.0, 20.0)
    residual_abs = np.abs(actual_log - oof_predicted_log)

    # Final fit on all rows.
    x_scaled, means, scales = _standardize(x)
    model.ridge_beta = _fit_ridge(x_scaled, y_residual, best_alpha)
    model.means, model.scales = means, scales
    model.ridge_alpha = float(best_alpha)
    if gbm_available and gbm_weight > 0.0:
        try:
            model.gbm = _make_gbm()
            model.gbm.fit(x, y_residual)
            model.used_gbm = True
        except Exception:
            model.gbm = None
            gbm_weight = 0.0
    model.gbm_weight = float(gbm_weight)

    # Current account baseline for future predictions.
    recent = y_log[-ROLLING_WINDOW:]
    model.current_baseline_log = float(np.median(recent))
    model.current_baseline_mean_log = float(np.mean(recent))
    model.last_published_at = str(rows[-1].get("published_at") or rows[-1].get("created_at") or "")

    model.oof_predicted_logs = oof_predicted_log.tolist()
    model.oof_actual_likes = actual_likes.tolist()
    model.interval_log_q80 = float(np.percentile(residual_abs, 80))
    model.interval_log_q90 = float(np.percentile(residual_abs, 90))
    model.train_median_likes = float(np.percentile(y_likes, 50))
    model.train_p75_likes = float(np.percentile(y_likes, 75))
    model.train_p90_likes = float(np.percentile(y_likes, 90))
    model.train_p95_likes = float(np.percentile(y_likes, 95))
    model.train_p99_likes = float(np.percentile(y_likes, 99))
    model.train_max_likes = float(np.max(y_likes))
    model.probability_priors = {
        key: float(np.mean(y_likes >= float(np.percentile(y_likes, pct))))
        for key, pct in [("median", 50), ("p75", 75), ("p90", 90)]
    }
    model.validation_strategy = f"expanding_window_{len(folds)}_folds"
    model.validation_count = int(len(oof_indices))
    model.mae_validation = metrics["mae"]
    model.log_mae_validation = metrics["log_mae"]
    model.wape_validation = metrics["wape"]
    model.r2_log_validation = metrics["r2_log"]
    model.spearman_validation = metrics["spearman"]
    model.ready = True
    model.message = (
        "multi_signal_v2 trained: hook + caption + TRIBE v2 + metadata + account-trend features, "
        "de-trended log-likes target, expanding-window OOF tuning"
        + (", GBM+ridge ensemble." if model.used_gbm else ", ridge (scikit-learn not installed).")
    )
    return model


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

def predict_multi_signal(post: dict[str, Any], model: MultiSignalModel) -> dict[str, Any] | None:
    if not model.ready or not post.get("analysis_summary"):
        return None
    if model.means is None or model.scales is None or model.ridge_beta is None:
        return None

    baseline_median = float(model.current_baseline_log or 0.0)
    baseline_mean = float(model.current_baseline_mean_log or baseline_median)
    days_prev = _days_since_last(post, model)
    x = np.array(
        [_feature_vector(post, model, baseline_median, baseline_mean, days_prev)],
        dtype=float,
    )
    x = np.clip(np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0), -1e6, 1e6)

    ridge_residual = float(_predict_ridge(x, model.means, model.scales, model.ridge_beta)[0])
    residual = ridge_residual
    if model.gbm is not None and model.gbm_weight > 0.0:
        gbm_residual = float(model.gbm.predict(x)[0])
        residual = (model.gbm_weight * gbm_residual) + ((1.0 - model.gbm_weight) * ridge_residual)

    max_log = math.log1p(max(float(model.train_max_likes or 0.0), 1.0)) + 0.35
    predicted_log = float(np.clip(baseline_median + residual, 0.0, max_log))
    predicted = max(0.0, math.expm1(predicted_log))

    q80 = float(model.interval_log_q80 or 0.45)
    q90 = float(model.interval_log_q90 or q80)
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
        "prediction_low": round(max(0.0, math.expm1(predicted_log - q80))),
        "prediction_high": round(max(predicted, math.expm1(predicted_log + q80))),
        "prediction_low_wide": round(max(0.0, math.expm1(predicted_log - q90))),
        "prediction_high_wide": round(max(predicted, math.expm1(predicted_log + q90))),
        "confidence": round(confidence, 2),
        "sample_count": model.sample_count,
        "model_version": model.model_version,
        "prediction_target": "detrended_log_likes_plus_account_baseline",
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
        "ridge_weight": round(1.0 - model.gbm_weight, 2),
        "gbm_weight": round(model.gbm_weight, 2),
        "account_baseline_likes": round(math.expm1(baseline_median)),
        "train_median_likes": model.train_median_likes,
        "train_p75_likes": model.train_p75_likes,
        "train_p90_likes": model.train_p90_likes,
        **{key: round(value, 3) for key, value in probabilities.items()},
    }


def _days_since_last(post: dict[str, Any], model: MultiSignalModel) -> float:
    target = _parse_datetime(post.get("published_at"))
    last = _parse_datetime(model.last_published_at)
    if target and last:
        return min(30.0, max(0.0, (target.timestamp() - last.timestamp()) / 86400.0))
    return 1.0


def _probability_above(predicted_log: float, threshold_likes: float, model: MultiSignalModel) -> float:
    oof_logs = np.array(model.oof_predicted_logs or [], dtype=float)
    oof_likes = np.array(model.oof_actual_likes or [], dtype=float)
    if oof_logs.size == 0 or oof_likes.size == 0:
        priors = model.probability_priors or {}
        if threshold_likes <= float(model.train_median_likes or 0.0):
            return float(priors.get("median", 0.5))
        if threshold_likes <= float(model.train_p75_likes or 0.0):
            return float(priors.get("p75", 0.25))
        return float(priors.get("p90", 0.1))
    distances = np.abs(oof_logs - predicted_log)
    bandwidth = max(float(np.std(oof_logs)) * 0.35, 0.12)
    weights = np.exp(-distances / bandwidth)
    local_prob = float(np.dot(weights, oof_likes >= threshold_likes) / max(float(np.sum(weights)), 1e-9))
    effective_n = float((np.sum(weights) ** 2) / max(np.sum(weights * weights), 1e-9))
    prior = float(np.mean(oof_likes >= threshold_likes))
    local_weight = min(0.85, effective_n / (effective_n + 18.0))
    return max(0.0, min(1.0, (local_weight * local_prob) + ((1.0 - local_weight) * prior)))


def _percentile_from_prediction(predicted_likes: float, model: MultiSignalModel) -> float:
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


def multi_signal_payload(model: MultiSignalModel) -> dict[str, Any]:
    data = {
        key: value
        for key, value in model.__dict__.items()
        if key
        not in {
            "means", "scales", "ridge_beta", "gbm",
            "oof_predicted_logs", "oof_actual_likes",
            "vocab_tags", "vocab_person", "vocab_company", "vocab_post_type",
            "feature_order",
        }
    }
    data["feature_count"] = len(model.feature_order)
    return data
