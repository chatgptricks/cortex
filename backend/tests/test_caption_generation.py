import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import httpx
from fastapi import HTTPException

from app import main


def _caption_db(tmp_path):
    database = tmp_path / "captions.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE accounts (
            handle TEXT PRIMARY KEY, label TEXT, group_name TEXT,
            is_canonical INTEGER, is_active INTEGER
        );
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY, shortcode TEXT, caption TEXT, title TEXT,
            published_at TEXT
        );
        CREATE TABLE dashboard_posts (
            id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
            caption TEXT, published_at TEXT
        );
        INSERT INTO accounts VALUES ('source', 'Source', 'competitors', 0, 1);
        INSERT INTO accounts VALUES ('ours', 'Our Brand', 'sentient', 0, 1);
        INSERT INTO accounts VALUES ('other', 'Other', 'competitors', 0, 1);
        INSERT INTO dashboard_posts VALUES (1, 'source', 'SRC1', 'Original caption', '2026-09-13T12:00:00+00:00');
        INSERT INTO dashboard_posts VALUES (2, 'ours', 'STYLE1', 'Our recent voice', '2026-09-13T11:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    @contextmanager
    def connect():
        value = sqlite3.connect(database)
        value.row_factory = sqlite3.Row
        try:
            yield value
            value.commit()
        finally:
            value.close()

    return connect


def test_caption_context_uses_owned_account_voice(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "connect", _caption_db(tmp_path))

    context = main._caption_generation_context("@source", "SRC1", "@ours")

    assert context["source_caption"] == "Original caption"
    assert context["target_account"] == "ours"
    assert context["target_label"] == "Our Brand"
    assert context["style_examples"] == ["Our recent voice"]


def test_caption_context_rejects_competitor_destination(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "connect", _caption_db(tmp_path))

    with pytest.raises(HTTPException) as error:
        main._caption_generation_context("source", "SRC1", "other")

    assert error.value.status_code == 400
    assert "Sentient" in str(error.value.detail)


def test_caption_generation_requires_server_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(HTTPException) as error:
        main._openai_caption_text({})

    assert error.value.status_code == 503


def test_caption_endpoint_returns_editable_result(monkeypatch):
    context = {"target_account": "ours"}
    monkeypatch.setattr(main, "_caption_generation_context", lambda *_: context)
    monkeypatch.setattr(main, "_openai_caption_text", lambda value: ("A genuinely new caption", "gpt-5-mini"))
    request = SimpleNamespace(state=SimpleNamespace(user_email="writer@example.com"))

    result = main.dashboard_generate_caption(request, "source", "SRC1", "ours")

    assert result == {
        "caption": "A genuinely new caption",
        "targetAccount": "ours",
        "model": "gpt-5-mini",
        "generatedBy": "writer@example.com",
    }


def test_openai_caption_request_is_stateless_and_parses_output(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Fresh caption"}]}]}

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setattr(httpx, "Client", Client)
    context = {
        "target_account": "ours",
        "target_label": "Our Brand",
        "source_caption": "Original caption",
        "style_examples": ["Our recent voice"],
    }

    caption, model = main._openai_caption_text(context)

    assert (caption, model) == ("Fresh caption", "gpt-5-mini")
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["json"]["store"] is False
    assert captured["json"]["input"].find("Original caption") >= 0
    assert captured["headers"]["Authorization"] == "Bearer test-secret"
