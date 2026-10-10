"""Read deployment-owned roster metadata without bundling personal data."""
from functools import lru_cache
import json
import os
from pathlib import Path
from typing import Any


_FLAG_FIELDS = {"is_dev", "can_access_news", "can_role_switch", "self_assignment_notifications"}
_ROLES = {"vc", "pd", "sales", "trainee", "dev"}


@lru_cache(maxsize=2)
def _parse_roster(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        raise RuntimeError("Private roster configuration could not be read.") from None
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise RuntimeError("Private roster configuration has an unsupported schema.")
    for field in ("slack_users_by_email", "slack_profile_images_by_user_id",
                  "queue_notification_slack_overrides", "dashboard_email_aliases",
                  "display_names", "slack_user_ids"):
        entries = value.get(field, {})
        if not isinstance(entries, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                for k, v in entries.items()):
            raise RuntimeError("Private roster configuration contains invalid mappings.")
    emails = value.get("dev_emails", [])
    if not isinstance(emails, list) or any(not isinstance(email, str) or "@" not in email for email in emails):
        raise RuntimeError("Private roster configuration contains invalid developer identities.")
    flags = value.get("user_flags", {})
    if not isinstance(flags, dict) or any(
        not isinstance(email, str) or not isinstance(entry, dict)
        or not set(entry).issubset(_FLAG_FIELDS) or any(type(flag) is not bool for flag in entry.values())
        for email, entry in flags.items()
    ):
        raise RuntimeError("Private roster configuration contains invalid capability flags.")
    overrides = value.get("upsert_roles", {})
    if not isinstance(overrides, dict) or any(
        not isinstance(email, str) or not isinstance(entry, dict)
        or not isinstance(entry.get("roles"), list)
        or any(not isinstance(role, str) or role not in _ROLES for role in entry["roles"])
        or type(entry.get("include_primary", False)) is not bool
        for email, entry in overrides.items()
    ):
        raise RuntimeError("Private roster configuration contains invalid role overrides.")
    migrations = value.get("seed_migrations", [])
    if not isinstance(migrations, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("marker"), str) or not entry["marker"].strip()
        for entry in migrations
    ):
        raise RuntimeError("Private roster configuration contains invalid migration identities.")
    return value


@lru_cache(maxsize=1)
def _read_roster(path: str, modified_ns: int) -> dict[str, Any]:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("Private roster configuration could not be read.") from exc
    return _parse_roster(content)


def roster() -> dict[str, Any]:
    """An absent roster grants no extra capability and seeds no identities."""
    configured = os.getenv("SENTIENT_ROSTER_FILE", "").strip()
    paths = [Path(configured)] if configured else [
        Path("/etc/secrets/sentient-roster.json"),
        Path(__file__).resolve().parent.parent / "sentient-roster.json",
    ]
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError("Private roster configuration is unavailable.") from exc
        return _read_roster(str(path.resolve()), stat.st_mtime_ns)
    return {}


def dev_emails() -> tuple[str, ...]:
    return tuple(str(email).strip().lower() for email in roster().get("dev_emails", []))


def user_flags(email: str, operating_roles: list[str] | tuple[str, ...] = ()) -> dict[str, bool]:
    clean = str(email or "").strip().lower()
    flags = dict(roster().get("user_flags", {}).get(clean, {}))
    stored_dev = isinstance(operating_roles, (list, tuple)) and "dev" in operating_roles
    flags["is_dev"] = bool(flags.get("is_dev") or clean in dev_emails() or stored_dev)
    return flags
