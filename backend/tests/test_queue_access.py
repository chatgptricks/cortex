from types import SimpleNamespace

from app import main


def _request(*, role: str, preview_active: bool) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(
        user_email="esteban@sentientagency.io",
        is_admin=False,
        is_dev=True,
        operating_role=role,
        operating_roles=[role, "pd"],
        queue_role_preview_active=preview_active,
    ))


def test_dev_admin_preview_keeps_queue_coordination_access():
    _, is_admin, roles = main._queue_v2_access(_request(role="admin", preview_active=True), coordinator=True)
    assert is_admin is True
    assert roles == ["admin", "pd"]


def test_dev_pd_preview_remains_restricted_for_queue_qa():
    _, is_admin, roles = main._queue_v2_access(_request(role="pd", preview_active=True))
    assert is_admin is False
    assert roles == ["pd", "pd"]


def test_dev_default_view_stays_coordinator_even_if_stored_role_is_pd():
    _, is_admin, _ = main._queue_v2_access(_request(role="pd", preview_active=False), coordinator=True)
    assert is_admin is True
