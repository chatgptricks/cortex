from __future__ import annotations

import pytest

from app import apify_sync


@pytest.mark.parametrize("handle", ["becauseofmarketing/", "two words", "name!", "a" * 31, "@", ".name", "name."])
def test_create_account_rejects_invalid_handles_before_database_or_apify(monkeypatch, handle: str) -> None:
    def unexpected_connect():
        raise AssertionError("invalid handles must be rejected before database access")

    monkeypatch.setattr("app.db.connect", unexpected_connect)

    with pytest.raises(apify_sync.ApifySyncError, match="valid Instagram username"):
        apify_sync.create_account(handle, handle, "competitors", 600)
