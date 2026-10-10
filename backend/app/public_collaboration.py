"""Public collaboration facts from stored, explicit Instagram coauthor metadata."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any


def _handle(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    handle = value.strip().removeprefix("@")
    return handle.lower() if re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle, re.ASCII) else None


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coauthors(value: Any, *, promoted: bool) -> tuple[list[str], bool] | None:
    if promoted and isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                return None
        else:
            value = [part.strip() for part in text.split(",")]
    if not isinstance(value, list):
        return None
    handles = []
    complete = True
    for entry in value:
        handle = _handle(entry.get("username") if isinstance(entry, dict) else entry)
        # Positive evidence survives bad entries; an incomplete list cannot
        # establish that there are no other participants.
        if handle is None:
            complete = False
            continue
        if handle not in handles:
            handles.append(handle)
    return handles, complete


def _classification(source: dict[str, Any], account: str) -> dict[str, Any] | None:
    raw = _payload(source.get("raw_json"))
    if "coauthorProducers" in raw:
        parsed = _coauthors(raw["coauthorProducers"], promoted=False)
    elif "coauthors" in source:
        parsed = _coauthors(source["coauthors"], promoted=True)
    else:
        return None
    if parsed is None:
        return None
    handles, complete = parsed
    participants = [handle for handle in handles if handle != account]
    owner = _handle(raw.get("ownerUsername"))
    if owner is None and isinstance(raw.get("owner"), dict):
        owner = _handle(raw["owner"].get("username"))
    if account in handles and owner and owner != account and owner not in participants:
        participants.append(owner)
    if participants:
        return {"is_collab": True, "collaborators": participants}
    if complete and (not handles or owner == account):
        return {"is_collab": False, "collaborators": []}
    # A self-only list may exclude the primary owner. Without that owner,
    # absence of another participant is not evidence of a non-collab post.
    return None


def _date(value: Any) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return datetime.min.replace(tzinfo=UTC)


def stored_collaboration(records: list[dict[str, Any]], account: str,
                         observation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Select newest usable explicit metadata; absent updates preserve evidence."""
    clean = _handle(account)
    if clean is None:
        return {"is_collab": None, "collaborators": []}
    candidates = []
    for index, record in enumerate(records):
        fact = _classification(record, clean)
        if fact is not None:
            stamp = _date(record.get("enriched_at") or record.get("updated_at"))
            candidates.append((stamp, record.get("_table") == "posts", -index, fact))
    if observation:
        raw = _payload(observation.get("raw_json"))
        codes = {str(record.get("shortcode") or "") for record in records}
        if all(raw.get(key) is None or str(raw[key]) in codes for key in ("shortCode", "shortcode")):
            fact = _classification(observation, clean)
            if fact is not None:
                candidates.append((_date(observation.get("observed_at")), True, 1, fact))
    selected = max(candidates, key=lambda value: value[:3])[3] if candidates else {"is_collab": None, "collaborators": []}
    return {"_account": clean, **selected}


def public_collaboration(source: dict[str, Any], account: str) -> dict[str, Any]:
    """Expose handles only; the internal provenance annotation never escapes."""
    clean = _handle(account)
    annotation = source.get("_public_collaboration")
    if isinstance(annotation, dict) and annotation.get("_account") == clean:
        fact = annotation
    else:
        fact = _classification(source, clean) if clean else None
    if fact is None:
        return {"is_collab": None, "collaborators": []}
    parsed = _coauthors(fact.get("collaborators"), promoted=False)
    status = fact.get("is_collab")
    if parsed is None or not parsed[1] or status is not None and not isinstance(status, bool):
        return {"is_collab": None, "collaborators": []}
    handles = parsed[0]
    participants = [handle for handle in handles if handle != clean]
    if status is True and not participants:
        return {"is_collab": None, "collaborators": []}
    return {"is_collab": status, "collaborators": participants if status is True else []}
