# 10 Predict — Enhancement Plan

Date: 2026-06-10. Scope: full project review plus a redesigned prediction algorithm that combines the hook, the caption, the TRIBE v2 brain-model results, post stats, and historical account data.

## 1. Current state (what the review found)

The app is a FastAPI + React tool that scores Instagram cover media with TRIBE v2 (an fMRI-like cortical response model), extracts hook text via OCR, stores posts in SQLite, and predicts likes with two parallel models:

- `calibration.py` — ridge on log-likes blended with KNN, tuned on a single holdout.
- `prediction_model.py` (`advanced_temporal_v1`) — ridge on log-likes blended with a post-type median baseline, temporal holdout, conformal-style intervals, and probability-above-percentile outputs.

Features used today: 7 global brain metrics, 8 network raw activations, `is_animated`, hook presence/token count, bag-of-words over the top 120 hook tokens, publish hour/weekday cyclic encodings, and one-hot vocabularies for tags, person, company, and post type.

## 2. Key gaps and opportunities

**The caption is stored but never used.** The `caption` column exists and is filled by the Instagram importer, but no feature is derived from it. This is the largest unused signal given the explicit goal.

**No account-trend signal.** Likes on a growing (or decaying) account drift over time; the model predicts absolute likes with no notion of "what this account gets lately." A rolling baseline of recent posts is typically the single strongest predictor and also removes the growth confound from every other coefficient.

**Brain output is under-used.** Only network `raw` values are consumed. The analysis JSONs also contain `top_regions`, network `score`s, `temporal_series`, `virality_potential`, and segment counts — none feed the model.

**Hook text is reduced to a sparse bag-of-words.** 120 binary token columns on a few hundred samples is noisy; engineered hook features (length, digits, question form, caps intensity) plus hashed token counts generalize better at this sample size.

**Two duplicated model stacks.** `calibration.py` and `prediction_model.py` re-implement standardization, ridge, validation splitting, and vocab building. Worse, `fit_calibration` does not filter `likes IS NOT NULL` and imputes missing likes with `FLOP_LIKES_BASELINE = 850`, silently training on fabricated targets.

**Single holdout validation.** Both models tune hyperparameters on one split. With small data this overfits the split; expanding-window cross-validation gives out-of-fold (OOF) predictions for every recent post and far more stable tuning, intervals, and probability calibration.

**`MIN_CALIBRATION_SAMPLES = 3`** lets a "trained" model ship with 3 samples. It should be ≥ 30 for the linear stack and the new model degrades gracefully below that.

**`comments` is stored but unused** — usable as a similarity/quality signal for historical neighbors (never as a feature of the post being predicted, since it is unknown pre-publish).

## 3. New algorithm — `multi_signal_v2` (implemented in `backend/app/prediction_v2.py`)

### 3.1 Signals and features

| Block | Features |
|---|---|
| Brain (TRIBE v2) | 7 global metrics; 8 network raw + 8 network 0–100 scores; top-region aggregates (mean/max raw of top 5, count above score 50); virality_potential when present |
| Hook | char/word count, has-digits, question form, exclamation, all-caps ratio, 32-dim hashed token counts |
| Caption | char/word/line counts, hashtag count, mention count, emoji count, question, CTA cue words (link-in-bio, comment, share, save, follow), 32-dim hashed token counts |
| Stats / metadata | is_animated, tags / person / company / post_type one-hots (min-freq 2), publish hour + weekday cyclic |
| Historical (account trend) | rolling median and mean of log-likes over the previous 20 posts, days since previous post, post recency index |

### 3.2 Target: de-trended log-likes

Train on `log1p(likes) − rolling_baseline_log` instead of raw log-likes. The model learns *content lift over the account's current normal*; at predict time the current rolling baseline is added back. This makes hook/caption/brain coefficients reflect content quality, not account growth.

### 3.3 Model and tuning

- Gradient boosting (`HistGradientBoostingRegressor`) when scikit-learn is available, ensembled with ridge; weight tuned on OOF predictions. Pure-NumPy ridge fallback keeps the dependency optional.
- Expanding-window cross-validation over time-ordered posts (up to 4 folds across the newest ~40%) replaces the single holdout. All tuning, metrics, intervals, and probabilities derive from OOF predictions.
- Conformal intervals from OOF residual quantiles (80/90%).
- Probability above median/p75/p90 via kernel-weighted OOF neighborhoods blended with the empirical prior (same contract as v1, better inputs).
- Output keeps the `predict_performance` response shape, so the frontend, A/B ranking, and LLM report need no changes. Fallback chain: `v2 → advanced_temporal_v1 → calibration`.

### 3.4 Why this should beat v1

1. Caption and account-trend signals are net-new information, not re-weightings of existing features.
2. De-trending removes the dominant non-content variance source, so the limited samples are spent learning content effects.
3. GBM captures interactions (e.g., visual-network activation matters more for Meme covers than News covers) that ridge cannot.
4. OOF-based tuning/calibration is far less likely to overfit one lucky split.

## 4. Roadmap beyond this change

**Near term.** Raise `MIN_CALIBRATION_SAMPLES`; delete the 850-likes imputation; consolidate the v1 stacks into the v2 feature builder; persist per-retrain metrics (model version, OOF MAE/Spearman, sample count) in a `model_runs` table so improvements are measurable over time.

**Mid term.** Replace hashed text features with sentence-embedding vectors for hook + caption (multilingual MiniLM, PCA to ~16 dims); add follower count at publish time if obtainable; multi-task on comments as an auxiliary target; weekly scheduled retrain + backtest report.

**Long term.** Engagement-velocity early feedback loop (likes at 1h after posting → revised forecast); thumbnail embedding (CLIP) alongside TRIBE features; Bayesian hierarchical model per post-type when sample counts justify it.

## 5. Acceptance criteria

- v2 trains whenever ≥ 30 completed historical posts with likes exist; otherwise the API transparently falls back to v1.
- OOF Spearman and log-MAE reported by `/api/prediction-model` improve over v1's temporal-holdout numbers on the same data.
- No frontend changes required; A/B ranking and reports keep working.
