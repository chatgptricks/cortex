from __future__ import annotations

import os
from pathlib import Path
from threading import Lock


OCR_MIN_CONFIDENCE = float(os.getenv("PREDICT_OCR_MIN_CONFIDENCE", "0.35"))
_PADDLE_OCR_ENGINE = None
_PADDLE_OCR_LOCK = Lock()
_RAPID_OCR_ENGINE = None
_RAPID_OCR_LOCK = Lock()


def extract_image_text(image_path: str | Path) -> str | None:
    if os.getenv("PREDICT_OCR_ENABLED", "1").lower() in {"0", "false", "no"}:
        return None

    path = Path(image_path)
    for provider in _ocr_providers():
        try:
            if provider == "paddle":
                text = _extract_paddle_text(path)
            elif provider == "rapidocr":
                text = _extract_rapidocr_text(path)
            else:
                continue
        except Exception:
            continue
        if text:
            return text

    return None


def extract_image_texts(image_paths: list[str | Path], batch_size: int = 100) -> list[str | None]:
    if not image_paths:
        return []
    size = max(1, batch_size)
    results: list[str | None] = []
    for start in range(0, len(image_paths), size):
        for image_path in image_paths[start : start + size]:
            results.append(extract_image_text(image_path))
    return results


def _ocr_providers() -> list[str]:
    raw = os.getenv("PREDICT_OCR_PROVIDER", "paddle")
    providers = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return providers or ["paddle"]


def _extract_paddle_text(image_path: Path) -> str | None:
    engine = _get_paddle_ocr_engine()
    result = engine.predict(str(image_path))
    lines = _paddle_lines(result)
    if not lines:
        return None
    return _clean_ocr_text("\n".join(lines)) or None


def _get_paddle_ocr_engine():
    global _PADDLE_OCR_ENGINE
    if _PADDLE_OCR_ENGINE is None:
        with _PADDLE_OCR_LOCK:
            if _PADDLE_OCR_ENGINE is None:
                os.environ.setdefault(
                    "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK",
                    os.getenv("PREDICT_OCR_DISABLE_MODEL_SOURCE_CHECK", "True"),
                )
                from paddleocr import PaddleOCR

                kwargs = {
                    "use_doc_orientation_classify": False,
                    "use_doc_unwarping": False,
                    "use_textline_orientation": False,
                    "lang": os.getenv("PREDICT_OCR_LANG", "en"),
                }
                optional_model_env = {
                    "text_detection_model_name": "PREDICT_PADDLE_TEXT_DETECTION_MODEL",
                    "text_detection_model_dir": "PREDICT_PADDLE_TEXT_DETECTION_MODEL_DIR",
                    "text_recognition_model_name": "PREDICT_PADDLE_TEXT_RECOGNITION_MODEL",
                    "text_recognition_model_dir": "PREDICT_PADDLE_TEXT_RECOGNITION_MODEL_DIR",
                }
                for key, env_name in optional_model_env.items():
                    value = os.getenv(env_name)
                    if value:
                        kwargs[key] = value
                _PADDLE_OCR_ENGINE = PaddleOCR(**kwargs)
    return _PADDLE_OCR_ENGINE


def _paddle_lines(result: object) -> list[str]:
    items: list[dict[str, float | str]] = []
    for page in result or []:
        data = _paddle_result_data(page)
        texts = data.get("rec_texts") if isinstance(data, dict) else None
        scores = data.get("rec_scores") if isinstance(data, dict) else None
        boxes = data.get("rec_boxes") if isinstance(data, dict) else None
        if not isinstance(texts, list) or not isinstance(scores, list) or boxes is None:
            continue
        for text, score, box in zip(texts, scores, boxes, strict=False):
            clean_text = str(text).strip()
            try:
                confidence = float(score)
                left, top, right, bottom = _box_bounds(box)
            except (TypeError, ValueError):
                continue
            if not clean_text or confidence < OCR_MIN_CONFIDENCE:
                continue
            height = max(1.0, bottom - top)
            items.append(
                {
                    "text": clean_text,
                    "confidence": confidence,
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "center_y": top + height / 2,
                    "height": height,
                }
            )
    if not items:
        return []

    lines: list[dict[str, object]] = []
    for item in sorted(items, key=lambda value: (float(value["center_y"]), float(value["left"]))):
        line = _matching_line(lines, item)
        if line is None:
            lines.append({"items": [item], "center_y": item["center_y"], "height": item["height"]})
            continue
        line_items = line["items"]
        if isinstance(line_items, list):
            line_items.append(item)
        line["center_y"] = _average_float(line_items, "center_y")
        line["height"] = max(float(part["height"]) for part in line_items if isinstance(part, dict))

    output: list[str] = []
    for line in sorted(lines, key=lambda value: float(value["center_y"])):
        line_items = line.get("items")
        if not isinstance(line_items, list):
            continue
        words = [
            str(part["text"])
            for part in sorted(line_items, key=lambda value: float(value["left"]))
            if isinstance(part, dict)
        ]
        if words:
            output.append(_clean_ocr_line(" ".join(words)))
    return output


def _paddle_result_data(page: object) -> dict[str, object]:
    json_value = getattr(page, "json", None)
    if isinstance(json_value, dict) and isinstance(json_value.get("res"), dict):
        return json_value["res"]
    if isinstance(page, dict):
        return page
    try:
        return dict(page)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {}


def _box_bounds(box: object) -> tuple[float, float, float, float]:
    values = box.tolist() if hasattr(box, "tolist") else box
    if not isinstance(values, (list, tuple)) or len(values) < 4:
        raise ValueError("Unsupported OCR box")
    if all(isinstance(value, (int, float)) for value in values[:4]):
        left, top, right, bottom = (float(value) for value in values[:4])
        return left, top, right, bottom
    points = [point for point in values if isinstance(point, (list, tuple)) and len(point) >= 2]
    if not points:
        raise ValueError("Unsupported OCR polygon")
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _matching_line(lines: list[dict[str, object]], item: dict[str, float | str]) -> dict[str, object] | None:
    center_y = float(item["center_y"])
    item_height = float(item["height"])
    for line in lines:
        line_center = float(line["center_y"])
        line_height = float(line["height"])
        tolerance = max(18.0, min(item_height, line_height) * 0.65)
        if abs(center_y - line_center) <= tolerance:
            return line
    return None


def _average_float(items: list[object], key: str) -> float:
    values = [float(item[key]) for item in items if isinstance(item, dict) and key in item]
    return sum(values) / len(values) if values else 0.0


def _clean_ocr_line(text: str) -> str:
    for before, after in {
        " ,": ",",
        " .": ".",
        " :": ":",
        " ;": ";",
        " !": "!",
        " ?": "?",
        "$ ": "$",
    }.items():
        text = text.replace(before, after)
    return " ".join(text.split())


def _extract_rapidocr_text(image_path: Path) -> str | None:
    engine = _get_rapidocr_engine()
    result, _elapsed = engine(str(image_path))
    lines = _rapidocr_lines(result)
    if not lines:
        return None
    return _clean_ocr_text("\n".join(lines)) or None


def _get_rapidocr_engine():
    global _RAPID_OCR_ENGINE
    if _RAPID_OCR_ENGINE is None:
        with _RAPID_OCR_LOCK:
            if _RAPID_OCR_ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR

                _RAPID_OCR_ENGINE = RapidOCR()
    return _RAPID_OCR_ENGINE


def _rapidocr_lines(result: object) -> list[str]:
    if not result:
        return []
    lines: list[str] = []
    for item in result:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        text = str(item[1]).strip()
        confidence = 1.0
        if len(item) >= 3:
            try:
                confidence = float(item[2])
            except (TypeError, ValueError):
                confidence = 1.0
        if text and confidence >= OCR_MIN_CONFIDENCE:
            lines.append(text)
    return lines


def _clean_ocr_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line and len(line) > 1]
    return "\n".join(lines)[:2000]
