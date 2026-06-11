from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import uuid
from bisect import bisect_right
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .calibration import fit_calibration, predict_likes
from .config import (
    ALLOWED_IMAGE_SUFFIXES,
    ANALYSIS_DIR,
    DATA_DIR,
    DEFAULT_VIDEO_SECONDS,
    EXTRA_CORS_ORIGINS,
    OCR_BATCH_MIN_READY,
    OCR_BATCH_SIZE,
    OCR_CROP_REGION,
    PREDICT_API_KEY,
    UPLOAD_DIR,
    VIDEO_DIR,
    ensure_directories,
)
from .db import connect, init_db, row_to_post, utc_now
from .instagram_import import InstagramImportError, fetch_instagram_post
from .llm_report import LlmReportUnavailable, generate_llm_report, llm_report_status
from .prediction_model import fit_advanced_prediction, predict_performance, prediction_payload
from .prediction_v2 import fit_multi_signal, multi_signal_payload, predict_multi_signal
from .remote_ocr import RemoteOcrUnavailable, extract_images_text_remote, remote_ocr_status
from .remote_tribe import RemoteTribeUnavailable, analyze_video_remote, remote_tribe_status
from .tribe_adapter import TribeUnavailable, analyze_video, tribe_status, write_analysis
from .video import create_static_video


app = FastAPI(title="Cortex API", version="1.0.0")

DEFAULT_PERSON_OPTIONS = [
    "Elon Musk",
    "Sam Altman",
    "Jensen Huang",
    "Dario Amodei",
    "Donald Trump",
    "Xi Jinping",
]
DEFAULT_COMPANY_OPTIONS = [
    "ChatGPT / OpenAI",
    "Claude / Anthropic",
    "Gemini / Google",
    "Grok / xAI",
]
DEFAULT_POST_TYPE_OPTIONS = ["Tricks", "News", "Promo", "Reel", "Meme"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", *EXTRA_CORS_ORIGINS],
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):517[0-9]$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _require_api_key(request, call_next):  # type: ignore[no-untyped-def]
    if PREDICT_API_KEY and request.method != "OPTIONS":
        path = request.url.path
        # /api/health stays open (Render health checks); /api/auth/check is the
        # login probe and validates the key itself.
        if (
            (path.startswith("/api") or path.startswith("/media"))
            and path not in {"/api/health", "/api/auth/check"}
        ):
            provided = request.headers.get("x-api-key") or request.query_params.get("token")
            if provided != PREDICT_API_KEY:
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "Invalid or missing API key."}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
def startup() -> None:
    init_db()
    _seed_default_metadata_options()


ensure_directories()
app.mount("/media", StaticFiles(directory=DATA_DIR), name="media")

_CALIBRATION_CACHE: dict[str, Any] = {}
_PREDICTION_MODEL_CACHE: dict[str, Any] = {}
_PREDICTION_V2_CACHE: dict[str, Any] = {}
_MODEL_LOG = logging.getLogger("uvicorn.error")
_FIT_LOCK = threading.Lock()


@app.get("/api/auth/check")
def auth_check(request: Request) -> dict[str, Any]:
    if not PREDICT_API_KEY:
        return {"auth_required": False, "ok": True}
    provided = request.headers.get("x-api-key") or request.query_params.get("token")
    return {"auth_required": True, "ok": provided == PREDICT_API_KEY}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "tribev2": tribe_status(),
        "llm_report": llm_report_status(),
        "remote_tribe": remote_tribe_status(),
        "remote_ocr": remote_ocr_status(),
    }


@app.get("/api/calibration")
def calibration() -> dict[str, Any]:
    posts = _all_posts()
    model = _fit_calibration_cached(posts)
    return _calibration_payload(model)


@app.get("/api/prediction-model")
def prediction_model() -> dict[str, Any]:
    posts = _all_posts()
    model = _fit_prediction_model_cached(posts)
    payload = prediction_payload(model)
    payload["multi_signal_v2"] = multi_signal_payload(_fit_prediction_v2_cached(posts))
    return payload


@app.post("/api/post-db/ocr/modal-batch")
def run_post_db_modal_ocr_batch(
    start: int = Query(default=1),
    limit: int = Query(default=OCR_BATCH_SIZE),
    min_ready: int = Query(default=OCR_BATCH_MIN_READY),
    crop_region: str = Query(default=OCR_CROP_REGION),
) -> dict[str, Any]:
    if limit < min_ready:
        raise HTTPException(status_code=400, detail=f"Modal OCR batch size must be at least {min_ready}.")
    eligible_count, rows = _eligible_modal_ocr_posts(start=start, limit=limit)
    if eligible_count < min_ready:
        raise HTTPException(
            status_code=400,
            detail=f"Modal OCR needs at least {min_ready} completed Post DB posts with blank OCR; {eligible_count} are ready.",
        )
    if len(rows) < min(limit, min_ready):
        raise HTTPException(status_code=400, detail="Some eligible image files are missing locally.")
    try:
        results = extract_images_text_remote([Path(row["image_path"]) for row in rows], crop_region=crop_region)
    except RemoteOcrUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    updated_ids: list[int] = []
    with connect() as conn:
        for row, result in zip(rows, results, strict=False):
            text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
            if not text:
                continue
            conn.execute(
                """
                UPDATE posts
                SET hook_text = ?, updated_at = ?
                WHERE id = ?
                  AND TRIM(COALESCE(hook_text, '')) = ''
                """,
                (text, utc_now(), int(row["id"])),
            )
            updated_ids.append(int(row["id"]))
    return {
        "eligible_count": eligible_count,
        "processed_count": len(results),
        "updated_count": len(updated_ids),
        "crop_region": crop_region,
        "posts": [decorate_post(_get_post_or_404(post_id)) for post_id in updated_ids],
    }


@app.get("/api/posts")
def list_posts(section: str | None = Query(default=None)) -> dict[str, Any]:
    if section == "historical":
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, section, title, published_at, likes,
                       person_label, company_label, post_type_label,
                       source_ref, source_row_number, shortcode,
                       image_path, original_filename, status, error,
                       progress_percent, progress_message, tags, hook_text,
                       is_animated, comments, created_at, updated_at,
                       analysis_path IS NOT NULL AS has_analysis_summary,
                       brain_global_mean_abs,
                       brain_global_peak_abs,
                       virality_potential
                FROM posts
                WHERE section = ?
                ORDER BY created_at DESC
                """,
                ("historical",),
            ).fetchall()
        posts = [_lightweight_historical_post(row_to_post(row)) for row in rows]
        return {"posts": posts, "calibration": {"ready": False, "sample_count": 0, "feature_order": []}}

    all_p = _all_posts()
    calib = _fit_calibration_cached(all_p)
    with connect() as conn:
        if section:
            rows = conn.execute(
                "SELECT * FROM posts WHERE section = ? ORDER BY created_at DESC",
                (section,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
    if section == "historical":
        posts = [_lightweight_historical_post(row_to_post(row)) for row in rows]
    else:
        percentile_values = _tribe_percentile_reference(all_p)
        pred_model = _fit_prediction_model_cached(all_p)
        posts = [
            decorate_post(row_to_post(row), all_p, calib, pred_model, percentile_values)
            for row in rows
        ]
    return {"posts": posts, "calibration": _calibration_payload(calib)}


@app.get("/api/posts/{post_id}")
def get_post(post_id: int) -> dict[str, Any]:
    post = _get_post_or_404(post_id)
    return {"post": decorate_post(post)}


@app.get("/api/metadata-options")
def metadata_options() -> dict[str, list[str]]:
    with connect() as conn:
        post_rows = conn.execute(
            """
            SELECT person_label, company_label, post_type_label, tags
            FROM posts
            """
        ).fetchall()
        option_rows = conn.execute("SELECT kind, label FROM metadata_options").fetchall()

    all_people = []
    all_companies = []
    all_tags = []
    
    def _parse_or_wrap(val: str | None) -> list[str]:
        if not val:
            return []
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(i) for i in parsed]
        except Exception:
            pass
        return [val]

    for row in post_rows:
        all_people.extend(_parse_or_wrap(row["person_label"]))
        all_companies.extend(_parse_or_wrap(row["company_label"]))
        all_tags.extend(_parse_or_wrap(row["tags"]))

    return {
        "people": _merged_options(
            DEFAULT_PERSON_OPTIONS,
            [row["label"] for row in option_rows if row["kind"] == "person"] + all_people,
        ),
        "companies": _merged_options(
            DEFAULT_COMPANY_OPTIONS,
            [row["label"] for row in option_rows if row["kind"] == "company"] + all_companies,
        ),
        "post_types": _merged_options(
            DEFAULT_POST_TYPE_OPTIONS,
            [row["label"] for row in option_rows if row["kind"] == "post_type"]
            + [row["post_type_label"] for row in post_rows],
        ),
        "tags": _merged_options([], all_tags),
    }


@app.post("/api/posts")
def create_post(
    background_tasks: BackgroundTasks,
    title: Annotated[str, Form()],
    section: Annotated[str, Form()] = "single",
    caption: Annotated[str | None, Form()] = None,
    published_at: Annotated[str | None, Form()] = None,
    likes: Annotated[str | None, Form()] = None,
    person_label: Annotated[str | None, Form()] = None,
    company_label: Annotated[str | None, Form()] = None,
    post_type_label: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    hook_text: Annotated[str | None, Form()] = None,
    is_animated: Annotated[bool, Form()] = False,
    comments: Annotated[int | None, Form()] = None,
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
    analyze_now: Annotated[bool, Form()] = True,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    if section not in {"single", "historical", "ab"}:
        raise HTTPException(status_code=400, detail="section must be single, historical, or ab")
    image_path = _save_upload(file)
    now = utc_now()
    clean_person = person_label
    clean_company = company_label
    clean_post_type = _clean_metadata_label(post_type_label)
    clean_hook_text = hook_text
    post_likes = _normalized_likes(likes, section)
    status, progress_percent, progress_message = _initial_post_state(section, analyze_now)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO posts (
                section, title, caption, published_at, likes,
                person_label, company_label, post_type_label,
                tags, hook_text, is_animated, comments,
                image_path, original_filename, status, progress_percent, progress_message,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                section,
                title.strip() or "Untitled cover",
                caption,
                published_at,
                post_likes,
                clean_person,
                clean_company,
                clean_post_type,
                tags,
                clean_hook_text,
                int(is_animated),
                comments,
                str(image_path),
                file.filename,
                status,
                progress_percent,
                progress_message,
                now,
                now,
            ),
        )
        post_id = int(cursor.lastrowid)
        _save_metadata_options(conn, clean_person, clean_company, clean_post_type)
    if analyze_now:
        background_tasks.add_task(run_analysis_job, post_id, duration_seconds)
    post = _get_post_or_404(post_id)
    return {"post": decorate_post(post)}


@app.post("/api/posts/instagram-link")
def create_post_from_instagram_link(
    background_tasks: BackgroundTasks,
    instagram_url: Annotated[str, Form()],
    cover_image_url: Annotated[str | None, Form()] = None,
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
    analyze_now: Annotated[bool, Form()] = True,
) -> dict[str, Any]:
    try:
        imported = fetch_instagram_post(instagram_url, cover_image_url=cover_image_url)
    except InstagramImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    source_ref = f"instagram:{imported.shortcode}"
    with connect() as conn:
        existing = conn.execute("SELECT * FROM posts WHERE source_ref = ?", (source_ref,)).fetchone()
    if existing:
        existing_post = row_to_post(existing)
        update_values: dict[str, Any] = {"updated_at": utc_now()}
        if imported.caption and not existing_post.get("caption"):
            update_values["caption"] = imported.caption
        if imported.title and (
            not existing_post.get("title")
            or str(existing_post.get("title")).startswith("Instagram post ")
        ):
            update_values["title"] = imported.title
        existing_image_path = Path(str(existing_post.get("image_path") or ""))
        if not existing_image_path.exists():
            image_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{imported.image_suffix}"
            image_path.write_bytes(imported.image_bytes)
            update_values["image_path"] = str(image_path)
            update_values["original_filename"] = f"instagram-{imported.shortcode}{imported.image_suffix}"
        if analyze_now and existing_post.get("status") not in {"queued", "running", "completed"}:
            update_values["status"] = "queued"
            update_values["error"] = None
            update_values["progress_percent"] = 5
            update_values["progress_message"] = "Queued"
        if len(update_values) > 1:
            assignments = ", ".join(f"{key} = ?" for key in update_values)
            with connect() as conn:
                conn.execute(
                    f"UPDATE posts SET {assignments} WHERE id = ?",
                    (*update_values.values(), int(existing_post["id"])),
                )
        if analyze_now and update_values.get("status") == "queued":
            background_tasks.add_task(run_analysis_job, int(existing_post["id"]), duration_seconds)
        return {"post": decorate_post(_get_post_or_404(int(existing_post["id"]))), "created": False}

    image_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{imported.image_suffix}"
    image_path.write_bytes(imported.image_bytes)
    now = utc_now()
    status, progress_percent, progress_message = _initial_post_state("single", analyze_now)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO posts (
                section, title, caption, source_ref, shortcode,
                image_path, original_filename, status, progress_percent,
                progress_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "single",
                imported.title,
                imported.caption,
                source_ref,
                imported.shortcode,
                str(image_path),
                f"instagram-{imported.shortcode}{imported.image_suffix}",
                status,
                progress_percent,
                progress_message,
                now,
                now,
            ),
        )
        post_id = int(cursor.lastrowid)

    if analyze_now:
        background_tasks.add_task(run_analysis_job, post_id, duration_seconds)
    return {"post": decorate_post(_get_post_or_404(post_id)), "created": True}


@app.post("/api/posts/batch")
def create_posts_batch(
    background_tasks: BackgroundTasks,
    section: Annotated[str, Form()] = "historical",
    titles: Annotated[str, Form()] = "[]",
    captions: Annotated[str, Form()] = "[]",
    published_ats: Annotated[str, Form()] = "[]",
    likes: Annotated[str, Form()] = "[]",
    person_labels: Annotated[str, Form()] = "[]",
    company_labels: Annotated[str, Form()] = "[]",
    post_type_labels: Annotated[str, Form()] = "[]",
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
    analyze_now: Annotated[bool, Form()] = False,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if section != "historical":
        raise HTTPException(status_code=400, detail="Batch upload is currently limited to Post DB posts.")
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one Post DB cover image.")

    title_values = _parse_metadata_list(titles, len(files))
    caption_values = _parse_metadata_list(captions, len(files))
    published_values = _parse_metadata_list(published_ats, len(files))
    like_values = _parse_metadata_list(likes, len(files))
    person_values = _parse_metadata_list(person_labels, len(files))
    company_values = _parse_metadata_list(company_labels, len(files))
    post_type_values = _parse_metadata_list(post_type_labels, len(files))

    now = utc_now()
    post_ids: list[int] = []
    with connect() as conn:
        for index, upload in enumerate(files):
            image_path = _save_upload(upload)
            post_title = title_values[index] or _title_from_filename(upload.filename) or f"Post DB cover {index + 1}"
            post_likes = _normalized_likes(like_values[index], "historical")
            clean_person = person_values[index]
            clean_company = company_values[index]
            clean_post_type = _clean_metadata_label(post_type_values[index])
            hook_text = None
            status, progress_percent, progress_message = _initial_post_state("historical", analyze_now)
            cursor = conn.execute(
                """
                INSERT INTO posts (
                    section, title, caption, published_at, likes,
                    person_label, company_label, post_type_label, hook_text,
                    image_path, original_filename, status, progress_percent,
                    progress_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "historical",
                    post_title,
                    caption_values[index] or None,
                    published_values[index] or None,
                    post_likes,
                    clean_person,
                    clean_company,
                    clean_post_type,
                    hook_text,
                    str(image_path),
                    upload.filename,
                    status,
                    progress_percent,
                    progress_message,
                    now,
                    now,
                ),
            )
            post_ids.append(int(cursor.lastrowid))
            _save_metadata_options(conn, clean_person, clean_company, clean_post_type)

    if analyze_now:
        background_tasks.add_task(run_batch_analysis_job, post_ids, duration_seconds)

    posts = [decorate_post(_get_post_or_404(post_id)) for post_id in post_ids]
    return {"posts": posts}


@app.patch("/api/posts/{post_id}")
def update_post(
    post_id: int,
    title: Annotated[str | None, Form()] = None,
    caption: Annotated[str | None, Form()] = None,
    published_at: Annotated[str | None, Form()] = None,
    likes: Annotated[str | None, Form()] = None,
    person_label: Annotated[str | None, Form()] = None,
    company_label: Annotated[str | None, Form()] = None,
    post_type_label: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    hook_text: Annotated[str | None, Form()] = None,
    is_animated: Annotated[bool | None, Form()] = None,
    comments: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    post = _get_post_or_404(post_id)
    fields = {
        "title": title,
        "caption": caption,
        "published_at": published_at,
        "person_label": person_label,
        "company_label": company_label,
        "post_type_label": _clean_metadata_label(post_type_label),
        "tags": tags,
        "hook_text": hook_text,
        "is_animated": int(is_animated) if is_animated is not None else None,
    }
    if likes is not None:
        fields["likes"] = _normalized_likes(likes, post.get("section") or "")
        if post.get("section") == "single":
            fields["section"] = "historical"
    if comments is not None and comments != "":
        fields["comments"] = _optional_int(comments)
    fields = {key: value for key, value in fields.items() if value is not None}
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE posts SET {assignments} WHERE id = ?",
            (*fields.values(), post_id),
        )
        _save_metadata_options(
            conn,
            fields.get("person_label"),
            fields.get("company_label"),
            fields.get("post_type_label"),
        )
    return {"post": decorate_post(_get_post_or_404(post_id))}


@app.delete("/api/posts/{post_id}")
def delete_post(post_id: int) -> dict[str, Any]:
    post = _get_post_or_404(post_id)
    file_paths = _post_file_paths(post)
    affected_ab_test_ids = _ab_test_ids_for_post(post_id)
    with connect() as conn:
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        _delete_empty_ab_tests(conn)
    for test_id in affected_ab_test_ids:
        _sync_ab_test_decision(test_id)
    deleted_files = _delete_owned_files(file_paths)
    return {"ok": True, "deleted_post_id": post_id, "deleted_files": deleted_files}


@app.post("/api/posts/{post_id}/analyze")
def analyze_post(
    post_id: int,
    background_tasks: BackgroundTasks,
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
) -> dict[str, Any]:
    _get_post_or_404(post_id)
    with connect() as conn:
        conn.execute(
            """
            UPDATE posts
            SET status = ?, error = NULL, progress_percent = ?, progress_message = ?,
                llm_report = NULL, updated_at = ?
            WHERE id = ?
            """,
            ("queued", 5, "Queued", utc_now(), post_id),
        )
    _sync_ab_tests_for_post(post_id)
    background_tasks.add_task(run_analysis_job, post_id, duration_seconds)
    return {"post": decorate_post(_get_post_or_404(post_id))}


@app.post("/api/posts/{post_id}/report")
def generate_post_report(
    post_id: int,
    force: bool = Query(default=False),
) -> dict[str, Any]:
    post = decorate_post(_get_post_or_404(post_id))
    if post.get("section") == "historical":
        raise HTTPException(
            status_code=400,
            detail="Post DB stores structured brain data and real likes; LLM text reports are disabled for this section.",
        )
    if post.get("status") != "completed" or not post.get("analysis_summary"):
        raise HTTPException(
            status_code=400,
            detail="Generate a completed TRIBE v2 analysis before requesting an LLM report.",
        )
    if post.get("llm_report") and not force:
        return {"report": post["llm_report"]}

    try:
        report = generate_llm_report(post, _calibration_payload(_fit_calibration_cached(_all_posts())))
    except LlmReportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with connect() as conn:
        conn.execute(
            "UPDATE posts SET llm_report = ?, updated_at = ? WHERE id = ?",
            (json.dumps(report), utc_now(), post_id),
        )
    return {"report": report}


@app.get("/api/ab-tests")
def list_ab_tests() -> dict[str, Any]:
    _sync_all_ab_test_decisions()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM ab_tests ORDER BY created_at DESC").fetchall()
    return {"tests": [dict(row) for row in rows]}


@app.post("/api/ab-tests")
def create_ab_test(
    background_tasks: BackgroundTasks,
    name: Annotated[str, Form()],
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
    candidate_titles: Annotated[str, Form()] = "[]",
    person_label: Annotated[str | None, Form()] = None,
    company_label: Annotated[str | None, Form()] = None,
    post_type_label: Annotated[str | None, Form()] = None,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Upload at least two covers for A/B testing")
    labels = _parse_candidate_titles(candidate_titles, len(files))
    clean_person = _clean_metadata_label(person_label)
    clean_company = _clean_metadata_label(company_label)
    clean_post_type = _clean_metadata_label(post_type_label)
    now = utc_now()
    with connect() as conn:
        _save_metadata_options(conn, clean_person, clean_company, clean_post_type)
        test_cursor = conn.execute(
            "INSERT INTO ab_tests (name, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name.strip() or "A/B test", "running", now, now),
        )
        test_id = int(test_cursor.lastrowid)
        post_ids = []
        for index, upload in enumerate(files):
            image_path = _save_upload(upload)
            label = labels[index] or f"Cover {index + 1}"
            post_cursor = conn.execute(
                """
                INSERT INTO posts (
                    section, title, person_label, company_label, post_type_label,
                    image_path, original_filename, status,
                    created_at, updated_at, progress_percent, progress_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ab",
                    label,
                    clean_person,
                    clean_company,
                    clean_post_type,
                    str(image_path),
                    upload.filename,
                    "queued",
                    now,
                    now,
                    5,
                    "Queued",
                ),
            )
            post_id = int(post_cursor.lastrowid)
            post_ids.append(post_id)
            conn.execute(
                "INSERT INTO ab_candidates (ab_test_id, post_id, label, created_at) VALUES (?, ?, ?, ?)",
                (test_id, post_id, label, now),
            )
    for post_id in post_ids:
        background_tasks.add_task(run_analysis_job, post_id, duration_seconds)
    return get_ab_test(test_id)


@app.get("/api/ab-tests/{test_id}")
def get_ab_test(test_id: int) -> dict[str, Any]:
    _sync_ab_test_decision(test_id)
    with connect() as conn:
        test = conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone()
        if not test:
            raise HTTPException(status_code=404, detail="A/B test not found")
        rows = conn.execute(
            """
            SELECT c.id AS candidate_id, c.label, p.*
            FROM ab_candidates c
            JOIN posts p ON p.id = c.post_id
            WHERE c.ab_test_id = ?
            ORDER BY c.id ASC
            """,
            (test_id,),
        ).fetchall()

    test_data = dict(test)
    all_p = _all_posts()
    calib = _fit_calibration_cached(all_p)
    pred_model = _fit_prediction_model_cached(all_p)
    candidates = [decorate_post(row_to_post(row), all_p, calib, pred_model) for row in rows]
    ranked = rank_candidates(
        candidates,
        winner_post_id=test_data.get("winner_post_id"),
        all_posts=all_p,
        calibration_model=calib,
        prediction_model=pred_model,
    )
    return {"test": test_data, "candidates": ranked}


@app.delete("/api/ab-tests/{test_id}")
def delete_ab_test(test_id: int) -> dict[str, Any]:
    with connect() as conn:
        test = conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone()
        if not test:
            raise HTTPException(status_code=404, detail="A/B test not found")
        rows = conn.execute(
            """
            SELECT p.*
            FROM ab_candidates c
            JOIN posts p ON p.id = c.post_id
            WHERE c.ab_test_id = ?
            """,
            (test_id,),
        ).fetchall()
        posts = [row_to_post(row) for row in rows]
        file_paths = [path for post in posts for path in _post_file_paths(post)]
        post_ids = [int(post["id"]) for post in posts]
        if post_ids:
            placeholders = ", ".join("?" for _ in post_ids)
            conn.execute(f"DELETE FROM posts WHERE id IN ({placeholders})", post_ids)
        conn.execute("DELETE FROM ab_tests WHERE id = ?", (test_id,))
    deleted_files = _delete_owned_files(file_paths)
    return {"ok": True, "deleted_test_id": test_id, "deleted_post_ids": post_ids, "deleted_files": deleted_files}


def run_batch_analysis_job(post_ids: list[int], duration_seconds: int = DEFAULT_VIDEO_SECONDS) -> None:
    for post_id in post_ids:
        run_analysis_job(post_id, duration_seconds)


def run_analysis_job(post_id: int, duration_seconds: int = DEFAULT_VIDEO_SECONDS) -> None:
    try:
        _set_post_progress(post_id, 10, "Preparing cover", status="running")
        post = _get_post_or_404(post_id)
        image_path = Path(post["image_path"])
        video_path = VIDEO_DIR / f"{post_id}-{uuid.uuid4().hex}.mp4"
        _set_post_progress(post_id, 22, "Converting image to video")
        create_static_video(image_path, video_path, duration_seconds=duration_seconds)
        if remote_tribe_status()["configured"]:
            _set_post_progress(post_id, 38, "Video ready; sending to remote GPU")
            summary = analyze_video_remote(video_path, duration_seconds=duration_seconds)
        else:
            _set_post_progress(post_id, 38, "Video ready; loading TRIBE v2")
            summary = analyze_video(video_path, duration_seconds=duration_seconds)
        _set_post_progress(post_id, 84, "Summarizing brain activations")
        analysis_path = ANALYSIS_DIR / f"{post_id}-{uuid.uuid4().hex}.json"
        write_analysis(analysis_path, summary)
        _set_post_progress(post_id, 94, "Saving results")
        with connect() as conn:
            metrics = summary.get("metrics") or {}
            conn.execute(
                """
                UPDATE posts
                SET video_path = ?, analysis_path = ?, analysis_summary = ?,
                    brain_global_mean_abs = ?, brain_global_peak_abs = ?,
                    virality_potential = ?,
                    status = ?, error = NULL, progress_percent = ?,
                    progress_message = ?, llm_report = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(video_path),
                    str(analysis_path),
                    json.dumps(summary),
                    metrics.get("global_mean_abs"),
                    metrics.get("global_peak_abs"),
                    summary.get("virality_potential"),
                    "completed",
                    100,
                    "Complete",
                    utc_now(),
                    post_id,
                ),
            )
        _sync_ab_tests_for_post(post_id)
    except (RemoteTribeUnavailable, TribeUnavailable, Exception) as exc:
        with connect() as conn:
            conn.execute(
                """
                UPDATE posts
                SET status = ?, error = ?, progress_message = ?, updated_at = ?
                WHERE id = ?
                """,
                ("failed", str(exc), "Analysis failed", utc_now(), post_id),
            )
        _sync_ab_tests_for_post(post_id)


def _set_post_progress(
    post_id: int,
    percent: int,
    message: str,
    status: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "progress_percent": max(0, min(100, int(percent))),
        "progress_message": message,
        "updated_at": utc_now(),
    }
    if status is not None:
        fields["status"] = status
        fields["error"] = None
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE posts SET {assignments} WHERE id = ?",
            (*fields.values(), post_id),
        )


def decorate_post(
    post: dict[str, Any],
    all_posts: list[dict[str, Any]] | None = None,
    calibration_model: Any = None,
    prediction_model: Any = None,
    percentile_values: list[float] | None = None,
    prediction_v2_model: Any = None,
) -> dict[str, Any]:
    if all_posts is None:
        all_posts = _all_posts()
    post["image_url"] = _media_url(post.get("image_path"))
    post["video_url"] = _media_url(post.get("video_path"))
    post["analysis_url"] = _media_url(post.get("analysis_path"))
    if post.get("section") == "historical":
        post["llm_report"] = None
        if post.get("analysis_summary") and "surface" in post["analysis_summary"]:
            del post["analysis_summary"]["surface"]
    if post.get("section") == "historical" or not post.get("analysis_summary"):
        # No prediction possible without an analysis — skip model fitting entirely
        # so imports/uploads return immediately.
        post["calibrated_prediction"] = None
    else:
        if calibration_model is None:
            calibration_model = _fit_calibration_cached(all_posts)
        if prediction_model is None:
            prediction_model = _fit_prediction_model_cached(all_posts)
        if prediction_v2_model is None:
            prediction_v2_model = _fit_prediction_v2_cached(all_posts)
        post["calibrated_prediction"] = (
            predict_multi_signal(post, prediction_v2_model)
            or predict_performance(post, prediction_model)
            or predict_likes(post, calibration_model, all_posts=all_posts)
        )
    post["tribe_percentile"] = _tribe_percentile(post, all_posts, percentile_values)
    return post


def _lightweight_historical_post(post: dict[str, Any]) -> dict[str, Any]:
    post["image_url"] = _media_url(post.get("image_path"))
    post["video_url"] = _media_url(post.get("video_path"))
    post["analysis_url"] = _media_url(post.get("analysis_path"))
    post["has_analysis_summary"] = bool(post.get("analysis_summary"))
    post["analysis_summary"] = None
    post["llm_report"] = None
    post["calibrated_prediction"] = None
    post["tribe_percentile"] = None
    return post


def _initial_post_state(section: str, analyze_now: bool) -> tuple[str, int, str | None]:
    if analyze_now:
        return "queued", 5, "Queued"
    if section == "historical":
        return "queued", 0, "Stored in Post DB; analysis pending"
    return "failed", 0, None


def rank_candidates(
    candidates: list[dict[str, Any]],
    winner_post_id: int | None = None,
    all_posts: list[dict[str, Any]] | None = None,
    calibration_model: Any = None,
    prediction_model: Any = None,
    prediction_v2_model: Any = None,
) -> list[dict[str, Any]]:
    if all_posts is None:
        all_posts = _all_posts()
    if calibration_model is None:
        calibration_model = _fit_calibration_cached(all_posts)
    if prediction_model is None:
        prediction_model = _fit_prediction_model_cached(all_posts)
    if prediction_v2_model is None:
        prediction_v2_model = _fit_prediction_v2_cached(all_posts)
    decorated = []
    for candidate in candidates:
        prediction = (
            predict_multi_signal(candidate, prediction_v2_model)
            or predict_performance(candidate, prediction_model)
            or predict_likes(candidate, calibration_model, all_posts=all_posts)
        )
        summary = candidate.get("analysis_summary") or {}
        metric = ((summary.get("metrics") or {}).get("global_mean_abs") or 0.0)
        ranking_value = prediction.get("ranking_value", prediction["predicted_likes"]) if prediction else metric
        candidate["ranking_value"] = ranking_value
        candidate["ranking_basis"] = "advanced_prediction" if prediction and prediction.get("model_version") else (
            "calibrated_likes" if prediction else "tribev2_global_activation"
        )
        decorated.append(candidate)
    decorated.sort(key=lambda item: item.get("ranking_value") or 0, reverse=True)
    for index, candidate in enumerate(decorated, start=1):
        candidate["rank"] = index
        candidate["is_winner"] = candidate.get("id") == winner_post_id
    return decorated


def _sync_all_ab_test_decisions() -> None:
    with connect() as conn:
        rows = conn.execute("SELECT id FROM ab_tests").fetchall()
    for row in rows:
        _sync_ab_test_decision(int(row["id"]))


def _sync_ab_tests_for_post(post_id: int) -> None:
    for test_id in _ab_test_ids_for_post(post_id):
        _sync_ab_test_decision(test_id)


def _ab_test_ids_for_post(post_id: int) -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT ab_test_id FROM ab_candidates WHERE post_id = ?",
            (post_id,),
        ).fetchall()
    return [int(row["ab_test_id"]) for row in rows]


def _sync_ab_test_decision(test_id: int) -> None:
    with connect() as conn:
        test = conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone()
        if not test:
            return
        rows = conn.execute(
            """
            SELECT p.*
            FROM ab_candidates c
            JOIN posts p ON p.id = c.post_id
            WHERE c.ab_test_id = ?
            ORDER BY c.id ASC
            """,
            (test_id,),
        ).fetchall()

    all_p = _all_posts()
    calib = _fit_calibration_cached(all_p)
    pred_model = _fit_prediction_model_cached(all_p)
    candidates = [decorate_post(row_to_post(row), all_p, calib, pred_model) for row in rows]
    if not candidates:
        with connect() as conn:
            conn.execute(
                "UPDATE ab_tests SET status = ?, winner_post_id = NULL, updated_at = ? WHERE id = ?",
                ("failed", utc_now(), test_id),
            )
        return

    if any(candidate.get("status") in {"queued", "running"} for candidate in candidates):
        with connect() as conn:
            conn.execute(
                "UPDATE ab_tests SET status = ?, winner_post_id = NULL, updated_at = ? WHERE id = ?",
                ("running", utc_now(), test_id),
            )
        return

    completed = [candidate for candidate in candidates if candidate.get("status") == "completed"]
    if not completed:
        with connect() as conn:
            conn.execute(
                "UPDATE ab_tests SET status = ?, winner_post_id = NULL, updated_at = ? WHERE id = ?",
                ("failed", utc_now(), test_id),
            )
        return

    winner = rank_candidates(
        completed,
        all_posts=all_p,
        calibration_model=calib,
        prediction_model=pred_model,
    )[0]
    with connect() as conn:
        conn.execute(
            "UPDATE ab_tests SET status = ?, winner_post_id = ?, updated_at = ? WHERE id = ?",
            ("completed", int(winner["id"]), utc_now(), test_id),
        )


def _all_posts() -> list[dict[str, Any]]:
    """Load all posts with heavy surface arrays stripped from historical rows.

    Surface data (20k+ vertices per post) is only needed by the 3D viewer for
    single/ab posts; loading it for 2k+ historical rows costs ~600MB of RAM.
    """
    with connect() as conn:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(posts)")]
        select_columns = ", ".join(
            (
                "CASE WHEN section = 'historical' "
                "THEN json_remove(analysis_summary, '$.surface') "
                "ELSE analysis_summary END AS analysis_summary"
            )
            if column == "analysis_summary"
            else column
            for column in columns
        )
        rows = conn.execute(f"SELECT {select_columns} FROM posts ORDER BY created_at DESC").fetchall()
    return [row_to_post(row) for row in rows]


def _fit_cached(
    posts: list[dict[str, Any]],
    cache: dict[str, Any],
    fitter: Any,
    label: str,
) -> Any:
    signature = _posts_signature(posts)
    cached = cache.get(signature)
    if cached is not None:
        return cached
    with _FIT_LOCK:
        cached = cache.get(signature)
        if cached is not None:
            return cached
        _MODEL_LOG.info("%s starting (n=%d)", label, len(posts))
        started = time.monotonic()
        model = fitter(posts)
        _MODEL_LOG.info("%s done in %.1fs", label, time.monotonic() - started)
        cache.clear()
        cache[signature] = model
        return model


def _fit_calibration_cached(posts: list[dict[str, Any]]) -> Any:
    return _fit_cached(posts, _CALIBRATION_CACHE, fit_calibration, "fit_calibration")


def _fit_prediction_model_cached(posts: list[dict[str, Any]]) -> Any:
    return _fit_cached(posts, _PREDICTION_MODEL_CACHE, fit_advanced_prediction, "fit_advanced_prediction")


def _fit_prediction_v2_cached(posts: list[dict[str, Any]]) -> Any:
    return _fit_cached(posts, _PREDICTION_V2_CACHE, fit_multi_signal, "fit_multi_signal")


def _posts_signature(posts: list[dict[str, Any]]) -> str:
    latest = max((str(post.get("updated_at") or "") for post in posts), default="")
    return f"{len(posts)}:{latest}"


def _calibration_payload(model: Any) -> dict[str, Any]:
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
        "train_vectors_scaled",
        "train_likes",
    ]:
        data.pop(key, None)
    return data


def _eligible_modal_ocr_posts(start: int, limit: int) -> tuple[int, list[dict[str, Any]]]:
    with connect() as conn:
        eligible_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM posts
                WHERE section = 'historical'
                  AND status = 'completed'
                  AND analysis_summary IS NOT NULL
                  AND TRIM(COALESCE(hook_text, '')) = ''
                  AND source_row_number >= ?
                """,
                (start,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT id, source_row_number, image_path
            FROM posts
            WHERE section = 'historical'
              AND status = 'completed'
              AND analysis_summary IS NOT NULL
              AND TRIM(COALESCE(hook_text, '')) = ''
              AND source_row_number >= ?
            ORDER BY source_row_number, id
            LIMIT ?
            """,
            (start, max(1, limit)),
        ).fetchall()

    records = []
    for row in rows:
        image_path = row["image_path"]
        if image_path and Path(image_path).exists():
            records.append(
                {
                    "id": int(row["id"]),
                    "source_row_number": int(row["source_row_number"] or 0),
                    "image_path": image_path,
                }
            )
    return eligible_count, records


def _get_post_or_404(post_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Post not found")
    return row_to_post(row)


def _save_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Upload a cover image with one of: {', '.join(sorted(ALLOWED_IMAGE_SUFFIXES))}",
        )
    output = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    with output.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)
    return output


def _post_file_paths(post: dict[str, Any]) -> list[Path]:
    paths = []
    for key in ["image_path", "video_path", "analysis_path"]:
        value = post.get(key)
        if value:
            paths.append(Path(value))
    return paths


def _delete_owned_files(paths: list[Path]) -> int:
    data_root = DATA_DIR.resolve()
    deleted = 0
    for path in {item.expanduser() for item in paths}:
        try:
            resolved = path.resolve()
            resolved.relative_to(data_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            resolved.unlink()
            deleted += 1
    return deleted


def _delete_empty_ab_tests(conn: Any) -> None:
    conn.execute(
        """
        DELETE FROM ab_tests
        WHERE id NOT IN (SELECT DISTINCT ab_test_id FROM ab_candidates)
        """
    )


def _media_url(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    try:
        relative = path.relative_to(DATA_DIR)
    except ValueError:
        return None
    return f"/media/{relative.as_posix()}"


def _parse_candidate_titles(raw: str, expected: int) -> list[str]:
    try:
        titles = json.loads(raw)
    except json.JSONDecodeError:
        titles = []
    if not isinstance(titles, list):
        titles = []
    titles = [str(item) for item in titles][:expected]
    while len(titles) < expected:
        titles.append("")
    return titles


def _parse_metadata_list(raw: str, expected: int) -> list[str]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        values = []
    if not isinstance(values, list):
        values = []
    normalized = ["" if item is None else str(item).strip() for item in values[:expected]]
    while len(normalized) < expected:
        normalized.append("")
    return normalized


def _clean_metadata_label(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        return None
    return cleaned[:80]


def _seed_default_metadata_options() -> None:
    with connect() as conn:
        for label in DEFAULT_PERSON_OPTIONS:
            _insert_metadata_option(conn, "person", label)
        for label in DEFAULT_COMPANY_OPTIONS:
            _insert_metadata_option(conn, "company", label)
        for label in DEFAULT_POST_TYPE_OPTIONS:
            _insert_metadata_option(conn, "post_type", label)


def _save_metadata_options(
    conn: Any,
    person_label: Any = None,
    company_label: Any = None,
    post_type_label: Any = None,
) -> None:
    def _insert_multiple(kind: str, val: Any) -> None:
        if not val:
            return
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                for item in parsed:
                    _insert_metadata_option(conn, kind, str(item))
                return
        except Exception:
            pass
        _insert_metadata_option(conn, kind, val)

    _insert_multiple("person", person_label)
    _insert_multiple("company", company_label)
    _insert_metadata_option(conn, "post_type", post_type_label)


def _insert_metadata_option(conn: Any, kind: str, label: Any) -> None:
    cleaned = _clean_metadata_label(str(label)) if label is not None else None
    if not cleaned:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO metadata_options (kind, label, slug, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(kind, slug) DO UPDATE SET
            label = excluded.label,
            updated_at = excluded.updated_at
        """,
        (kind, cleaned, _metadata_slug(cleaned), now, now),
    )


def _metadata_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
    return slug or uuid.uuid4().hex


def _merged_options(defaults: list[str], values: list[str | None]) -> list[str]:
    options: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        cleaned = _clean_metadata_label(value)
        if not cleaned:
            return
        key = cleaned.casefold()
        if key in seen:
            return
        seen.add(key)
        options.append(cleaned)

    for default in defaults:
        add(default)
    custom = sorted(
        [item for item in (_clean_metadata_label(value) for value in values) if item],
        key=str.casefold,
    )
    for value in custom:
        add(value)
    return options


FLOP_LIKES_BASELINE = 850


def _normalized_likes(raw: Any, section: str) -> int | None:
    likes = _optional_int(raw)
    if section == "historical" and likes is None:
        return FLOP_LIKES_BASELINE
    return likes


def _optional_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _title_from_filename(filename: str | None) -> str:
    if not filename:
        return ""
    return Path(filename).stem.replace("_", " ").replace("-", " ").strip().title()


def _tribe_percentile_reference(all_posts: list[dict[str, Any]]) -> list[float]:
    values = []
    for row in all_posts:
        if (
            row.get("section") == "historical"
            and row.get("status") == "completed"
            and row.get("analysis_summary")
        ):
            value = ((row["analysis_summary"].get("metrics") or {}).get("global_mean_abs") or 0.0)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = 0.0
            if numeric == numeric and numeric not in {float("inf"), float("-inf")}:
                values.append(numeric)
    values.sort()
    return values


def _tribe_percentile(
    post: dict[str, Any],
    all_posts: list[dict[str, Any]] | None = None,
    percentile_values: list[float] | None = None,
) -> float | None:
    summary = post.get("analysis_summary")
    if not summary:
        return None
    value = (summary.get("metrics") or {}).get("global_mean_abs")
    if value is None:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    if numeric_value != numeric_value or numeric_value in {float("inf"), float("-inf")}:
        return None
    if percentile_values is None and all_posts is None:
        all_posts = _all_posts()
    if percentile_values is None:
        percentile_values = _tribe_percentile_reference(all_posts or [])
    if len(percentile_values) < 2:
        return None
    below_or_equal = bisect_right(percentile_values, numeric_value)
    return round(100 * below_or_equal / len(percentile_values), 1)
