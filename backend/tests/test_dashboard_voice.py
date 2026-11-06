import base64
from contextlib import contextmanager
import io
import json
import sqlite3
import wave

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app import dashboard_voice as voice

WEBM = b"\x1aE\xdf\xa3" + b"sample" * 8


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    voice._usage.clear()
    voice._active.clear()
    monkeypatch.setattr(voice, "_transcription_context", lambda: {"prompt": "Dashboard post questions", "keywords[]": ["Sentient", "chatgptricks"]})
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.user_email = request.headers.get("x-email", "tester@example.test")
        request.state.credential_kind = request.headers.get("x-kind", "firebase")
        request.state.operating_role = request.headers.get("x-role", "pd")
        return await call_next(request)

    app.include_router(voice.router)
    with TestClient(app) as test:
        yield test


@pytest.mark.parametrize("role", ["viewer", "pd", "vc", "dev", "admin"])
def test_every_signed_in_role_can_transcribe_and_play_voice(client, monkeypatch, role):
    async def transcribe(data, kind, mime, language, key):
        assert (data, kind, language, key) == (WEBM, "webm", "es", "test-secret")
        return "Muéstrame los mejores reels."

    async def synthesize(body, results, key):
        assert body.language == "es" and body.mode == "next"
        return b"RIFF-audio", "Aquí tienes el resultado."

    monkeypatch.setattr(voice, "_transcribe", transcribe)
    monkeypatch.setattr(voice, "_synthesize", synthesize)
    response = client.post("/api/dashboard/assistant/transcribe", headers={"x-role": role},
                           files={"file": ("voice.webm", WEBM, "audio/webm;codecs=opus")}, data={"language": "es"})
    assert response.status_code == 200 and response.json()["text"].startswith("Muéstrame")
    response = client.post("/api/dashboard/assistant/speech", headers={"x-role": role},
                           json={"text": "Aquí tienes el resultado", "question": "/next reel", "language": "es", "mode": "next"})
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "private, no-store"
    assert base64.b64decode(response.headers["x-sentient-speech-transcript"]).decode("utf-8") == "Aquí tienes el resultado."
    assert "test-secret" not in response.text
    assert not voice._active


@pytest.mark.parametrize("kind", ["agent_code", "api_key", ""])
def test_voice_requires_browser_identity(client, kind):
    headers = {"x-kind": kind}
    assert client.post("/api/dashboard/assistant/transcribe", headers=headers,
                       files={"file": ("voice.webm", WEBM, "audio/webm")}).status_code == 403
    assert client.post("/api/dashboard/assistant/speech", headers=headers, json={"text": "Answer"}).status_code == 403


def test_missing_configuration_and_empty_answers(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    assert client.post("/api/dashboard/assistant/speech", json={"text": "Answer"}).status_code == 503
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    assert client.post("/api/dashboard/assistant/speech", json={"text": "   "}).status_code == 422
    assert client.post("/api/dashboard/assistant/speech", json={"text": "Answer", "voice": "unexpected"}).status_code == 422


def test_voice_catalogue_has_ordered_allowlist_validated_default_and_fixed_samples(client, monkeypatch):
    monkeypatch.setenv("OPENAI_VOICE", "coral")
    response = client.get("/api/dashboard/assistant/voices")
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    payload = response.json()
    expected = ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"]
    assert payload["voices"] == [{"id": name, "name": name.title()} for name in expected]
    assert payload["default_voice"] == "coral"
    assert payload["samples"] == voice._VOICE_SAMPLES
    assert voice.VoicePreviewRequest.model_json_schema()["properties"]["voice"]["enum"] == expected
    monkeypatch.setenv("OPENAI_VOICE", "invalid-server-config")
    assert client.get("/api/dashboard/assistant/voices").json()["default_voice"] == "marin"
    assert client.get("/api/dashboard/assistant/voices", headers={"x-kind": "agent_code"}).status_code == 403


@pytest.mark.parametrize("language,sample,selected", [("es", "next", "coral"), ("en", "results", "cedar")])
def test_voice_preview_uses_fixed_verbatim_sample_without_dashboard_lookup(client, monkeypatch, language, sample, selected):
    narration = voice._VOICE_SAMPLES[sample][language]
    payload = events()
    payload.insert(-1, {"type": "response.output_audio_transcript.done", "transcript": narration})
    socket = Socket(payload)
    monkeypatch.setenv("OPENAI_VOICE", "marin")
    monkeypatch.setattr(voice, "websocket_connect", lambda *args, **kwargs: socket)
    monkeypatch.setattr(voice, "_narration", lambda *args: pytest.fail("Preview must not use answer narration"))
    monkeypatch.setattr(voice, "_stored_results", lambda *args: pytest.fail("Preview must not read posts"))
    monkeypatch.setattr(voice.db, "connect", lambda: pytest.fail("Preview must not query the database"))
    response = client.post("/api/dashboard/assistant/voice-preview", json={"voice": selected, "language": language, "sample": sample})
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    assert response.content.startswith(b"RIFF") and response.headers["cache-control"] == "private, no-store"
    assert base64.b64decode(response.headers["x-sentient-speech-transcript"]).decode("utf-8") == narration
    assert socket.sent[0]["session"]["audio"]["output"]["voice"] == selected
    assert "VERBATIM" in socket.sent[0]["session"]["instructions"]
    assert socket.sent[1]["item"]["content"] == [{"type": "input_text", "text": narration}]
    assert socket.closed and not voice._active
    assert voice._default_voice() == "marin"


@pytest.mark.parametrize("changes", [{"voice": "unknown"}, {"language": "fr"}, {"sample": "custom"}, {"text": "arbitrary content"}])
def test_voice_preview_rejects_unsupported_schema_before_provider(client, monkeypatch, changes):
    monkeypatch.setattr(voice, "_synthesize", lambda *args, **kwargs: pytest.fail("Invalid preview must not contact provider"))
    body = {"voice": "coral", "language": "es", "sample": "results", **changes}
    assert client.post("/api/dashboard/assistant/voice-preview", json=body).status_code == 422


def test_preview_uses_same_browser_auth_and_shared_busy_guard(client, monkeypatch):
    body = {"voice": "coral", "language": "es", "sample": "results"}
    assert client.post("/api/dashboard/assistant/voice-preview", headers={"x-kind": "agent_code"}, json=body).status_code == 403
    voice._active.add("tester@example.test")
    assert client.post("/api/dashboard/assistant/voice-preview", json=body).status_code == 429
    voice._active.clear()
    async def timeout(*args, **kwargs):
        raise TimeoutError()
    monkeypatch.setattr(voice, "_synthesize", timeout)
    assert client.post("/api/dashboard/assistant/voice-preview", json=body).status_code == 504
    assert not voice._active


def test_personal_voice_override_changes_speech_session_without_server_default(client, monkeypatch):
    monkeypatch.setenv("OPENAI_VOICE", "marin")
    socket = Socket(events())
    monkeypatch.setattr(voice, "websocket_connect", lambda *args, **kwargs: socket)
    response = client.post("/api/dashboard/assistant/speech", json={"text": "Existing answer", "voice": "coral"})
    assert response.status_code == 200
    assert socket.sent[0]["session"]["audio"]["output"]["voice"] == "coral"
    assert voice._default_voice() == "marin"


@pytest.mark.parametrize("data,mime,status", [(b"", "audio/webm", 422), (WEBM, "image/png", 415),
                                             (b"fake recording", "audio/webm", 415)])
def test_invalid_uploads_never_contact_provider(client, monkeypatch, data, mime, status):
    monkeypatch.setattr(voice, "_transcribe", lambda *args: pytest.fail("No provider call"))
    response = client.post("/api/dashboard/assistant/transcribe", files={"file": ("audio.webm", data, mime)})
    assert response.status_code == status
    assert not voice._active


def test_upload_size_language_and_memory_only_cleanup(client, monkeypatch):
    monkeypatch.setattr(voice, "MAX_UPLOAD", 128)
    response = client.post("/api/dashboard/assistant/transcribe", files={"file": ("audio.webm", WEBM * 4, "audio/webm")})
    assert response.status_code == 413
    assert client.post("/api/dashboard/assistant/transcribe", files={"file": ("audio.webm", WEBM, "audio/webm")},
                       data={"language": "fr"}).status_code == 422
    assert client.post("/api/dashboard/assistant/transcribe", content=b"x", headers={"content-type": "audio/webm"}).status_code == 415
    parsers = []
    base = voice.MultiPartParser

    class Parser(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            parsers.append(self)

    async def transcribe(*args):
        return "Hello"

    monkeypatch.setattr(voice, "MultiPartParser", Parser)
    monkeypatch.setattr(voice, "_transcribe", transcribe)
    assert client.post("/api/dashboard/assistant/transcribe", files={"file": ("audio.webm", WEBM, "audio/webm")}).status_code == 200
    assert parsers[-1]._files_to_close_on_error
    assert all(file.closed and not file._rolled for file in parsers[-1]._files_to_close_on_error)


def test_rate_and_concurrency_guards_release_after_errors(client, monkeypatch):
    voice._active.add("tester@example.test")
    assert client.post("/api/dashboard/assistant/speech", json={"text": "Answer"}).status_code == 429
    voice._active.clear()
    async def timeout(*args):
        raise TimeoutError()
    monkeypatch.setattr(voice, "_synthesize", timeout)
    assert client.post("/api/dashboard/assistant/speech", json={"text": "Answer"}).status_code == 504
    assert not voice._active
    voice._usage["tester@example.test"] = __import__("collections").deque([voice.time.monotonic()] * 20)
    assert client.post("/api/dashboard/assistant/speech", json={"text": "Answer"}).status_code == 429


def test_transcription_provider_contract(monkeypatch):
    observed = []
    def provider(request):
        observed.append(request)
        return httpx.Response(200, json={"text": " latest post "})
    original = httpx.AsyncClient
    monkeypatch.setattr(voice, "_transcription_context", lambda: {"prompt": "Dashboard accounts", "keywords[]": ["Sentient", "chatgptricks"]})
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    result = anyio.run(voice._transcribe, WEBM, "webm", "audio/webm", "en", "secret")
    assert result == "latest post"
    assert observed[0].url.path == "/v1/audio/transcriptions"
    assert b'gpt-transcribe' in observed[0].content and b'name="languages[]"' in observed[0].content
    assert b'name="language"' not in observed[0].content
    assert observed[0].content.count(b'name="keywords[]"') == 2
    assert b'chatgptricks' in observed[0].content and b'name="prompt"' in observed[0].content


def test_transcription_context_reads_only_active_handles_and_sanitized_labels(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE accounts(handle TEXT, label TEXT, is_active INTEGER, group_name TEXT)")
    conn.executemany("INSERT INTO accounts VALUES(?,?,?,?)", [
        ("chatgptricks", "ChatGPT Tricks", 1, "sentient"), ("evolving.ai", "Evolving AI", 1, "competitors"),
        ("hidden", "Inactive Label", 0, "sentient"), ("unsafe", "bad\nkeyword", 1, "competitors"),
        ("markup", "<bad>", 1, "competitors"),
        *[(f"account{i:03}", "Label " + str(i), 1, "competitors") for i in range(100)]])
    conn.commit()
    conn.execute("PRAGMA query_only = ON")
    @contextmanager
    def connect():
        yield conn
    monkeypatch.setattr(voice.db, "connect", connect)
    context = voice._transcription_context()
    assert "chatgptricks" in context["keywords[]"] and "ChatGPT Tricks" in context["keywords[]"]
    assert "hidden" not in context["keywords[]"] and "Inactive Label" not in context["keywords[]"]
    assert len(context["keywords[]"]) <= 100
    assert not any(any(character in term for character in "<>\r\n") for term in context["keywords[]"])
    conn.close()


def test_transcription_context_failure_keeps_bounded_common_hints(monkeypatch):
    def unavailable():
        raise RuntimeError("private database detail")
    monkeypatch.setattr(voice.db, "connect", unavailable)
    context = voice._transcription_context()
    assert context["keywords[]"] == list(voice._TRANSCRIPTION_TERMS)
    assert "private" not in json.dumps(context)


@pytest.mark.parametrize("status,payload,expected", [(429, {"error": "secret"}, 429), (500, {"error": "secret"}, 502),
                                                    (200, {"text": ""}, 422), (200, {"text": "x" * 4001}, 422), (200, [], 502)])
def test_transcription_provider_failures_are_safe(client, monkeypatch, status, payload, expected):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    response = client.post("/api/dashboard/assistant/transcribe", files={"file": ("audio.webm", WEBM, "audio/webm")})
    assert response.status_code == expected and "secret" not in response.text
    assert not voice._active


class Socket:
    def __init__(self, events):
        self.events = list(events)
        self.sent = []
        self.closed = False
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        self.closed = True
    async def send(self, message):
        self.sent.append(json.loads(message))
    def __aiter__(self):
        return self
    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return json.dumps(self.events.pop(0))


def events(data=b"\0\0" * 80, status="completed"):
    return [{"type": "session.created"}, {"type": "session.updated"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(data).decode()},
            {"type": "response.done", "response": {"status": status}}]


def test_realtime_pcm_wav_and_grounded_narration(monkeypatch):
    socket = Socket(events())
    connections = []
    def connect(url, **kwargs):
        connections.append((url, kwargs))
        return socket
    monkeypatch.setattr(voice, "websocket_connect", connect)
    body = voice.SpeechRequest(text="Recrea [este reel](https://sentientdash.app/index.html?post=rival%3AClip): explica el experimento con claridad.",
                               question="/next reel para alpha", mode="next", language="es")
    stored = [{"account": "rival", "caption": "A robot folds laundry", "likes": 3200}]
    audio, transcript = anyio.run(voice._synthesize, body, stored, "private-key")
    assert transcript == ""
    with wave.open(io.BytesIO(audio)) as output:
        assert (output.getnchannels(), output.getsampwidth(), output.getframerate(), output.getnframes()) == (1, 2, 24000, 80)
    assert socket.closed
    assert "gpt-realtime-2.1-mini" in connections[0][0]
    session = socket.sent[0]["session"]
    assert session["audio"]["output"]["voice"] == "marin" and session["tools"] == []
    assert "not a new assistant" in session["instructions"] and "Do not use general knowledge" in session["instructions"]
    content = socket.sent[1]["item"]["content"][0]["text"]
    assert "A robot folds laundry" in content and "http" not in content
    assert json.loads(content)["stored_results"] == stored
    assert socket.sent[2]["type"] == "response.create"


@pytest.mark.parametrize("language", ["en", "es"])
def test_recommendation_narration_prioritizes_actual_plot_and_separate_attribution(language):
    body = voice.SpeechRequest(
        text="[Post](https://sentientdash.app/index.html?post=evolving.ai%3AStory) — 6,562 likes. Its escalating action makes it a strong storytelling reference.",
        question="/next reel para chatgptricks", mode="next", language=language)
    source = {"account": "evolving.ai", "shortcode": "Story", "likes": 6562,
              "caption": "A platypus escapes crocodiles and wolves, jumps from a cliff, and lands on a motorcycle.", "ocr_text": ""}
    instructions, serialized = voice._narration(body, [source])
    payload = json.loads(serialized)
    assert "LEAD with the concrete source story" in instructions
    assert "SOURCE account from stored_results.account" in instructions
    assert "separately identify the DESTINATION" in instructions
    assert "Metrics belong to the source post" in instructions
    assert "Usually omit metrics" in instructions and "Never say 'next', '/next'" in instructions
    assert "Do not invent creators" in instructions and "reasons beyond the supplied evidence" in instructions
    assert "at most 85 words" in instructions
    assert payload["stored_results"] == [source] and payload["has_post_links"] is True
    assert "escalating action" in payload["existing_answer"] and "https://" not in payload["existing_answer"]
    # The story remains request data, never a canned plot inserted in every narration.
    assert "platypus" not in instructions and "Pixar" not in instructions


@pytest.mark.parametrize("payload", [events(status="incomplete"), events(b"x"),
                                     [{"type": "session.updated"}, {"type": "error", "error": {"message": "secret"}}],
                                     [{"type": "session.updated"}, {"type": "response.output_audio.delta", "delta": "invalid!"}],
                                     [{"type": "session.updated"}, {"type": "response.done", "response": None}],
                                     [{"type": "session.updated"}]])
def test_realtime_failure_closes_socket_and_returns_no_partial_audio(client, monkeypatch, payload):
    socket = Socket(payload)
    monkeypatch.setattr(voice, "websocket_connect", lambda *args, **kwargs: socket)
    response = client.post("/api/dashboard/assistant/speech", json={"text": "Here are the results"})
    assert response.status_code == 502 and "secret" not in response.text
    assert socket.closed and not voice._active


def test_realtime_audio_size_bound(client, monkeypatch):
    monkeypatch.setattr(voice, "MAX_PCM", 4)
    socket = Socket(events(b"\0" * 6))
    monkeypatch.setattr(voice, "websocket_connect", lambda *args, **kwargs: socket)
    assert client.post("/api/dashboard/assistant/speech", json={"text": "Answer"}).status_code == 502
    assert socket.closed


def test_exact_provider_transcript_header_is_ascii_bounded_and_wav_stays_unchanged(client, monkeypatch):
    narration = "Aquí están los resultados: el ornitorrinco escapa de los cocodrilos. " * 30
    payload = events()
    payload.insert(-1, {"type": "response.output_audio_transcript.done", "transcript": narration})
    socket = Socket(payload)
    monkeypatch.setattr(voice, "websocket_connect", lambda *args, **kwargs: socket)
    response = client.post("/api/dashboard/assistant/speech", json={"text": "Existing answer", "language": "es"})
    assert response.status_code == 200 and response.content.startswith(b"RIFF")
    assert response.headers["x-sentient-speech-transcript-encoding"] == "base64-utf8"
    encoded = response.headers["x-sentient-speech-transcript"]
    assert encoded.isascii()
    assert base64.b64decode(encoded).decode("utf-8") == " ".join(narration.split())[:1200]
    assert "cocodrilos" not in encoded


def test_transcript_can_be_recovered_from_completed_response(monkeypatch):
    payload = events()
    payload[-1]["response"]["output"] = [{"content": [{"type": "audio", "transcript": "Here are the results."}]}]
    socket = Socket(payload)
    monkeypatch.setattr(voice, "websocket_connect", lambda *args, **kwargs: socket)
    audio, transcript = anyio.run(voice._synthesize, voice.SpeechRequest(text="Existing answer"), [], "secret")
    assert audio.startswith(b"RIFF") and transcript == "Here are the results."


def test_realtime_rate_limit_does_not_expose_provider_error(client, monkeypatch):
    socket = Socket([{"type": "error", "error": {"code": "rate_limit_exceeded", "message": "private organization token detail"}}])
    monkeypatch.setattr(voice, "websocket_connect", lambda *args, **kwargs: socket)
    response = client.post("/api/dashboard/assistant/speech", json={"text": "Answer"})
    assert response.status_code == 429 and response.headers["retry-after"] == "15"
    assert "organization" not in response.text and socket.closed


def test_realtime_cancellation_closes_socket(monkeypatch):
    class WaitingSocket(Socket):
        async def __anext__(self):
            await anyio.sleep_forever()
    socket = WaitingSocket([])
    monkeypatch.setattr(voice, "websocket_connect", lambda *args, **kwargs: socket)
    async def run():
        with anyio.fail_after(.01):
            await voice._synthesize(voice.SpeechRequest(text="Answer"), [], "secret")
    with pytest.raises(TimeoutError):
        anyio.run(run)
    assert socket.closed


def test_disconnected_request_cancels_provider_and_releases_connection(monkeypatch):
    class Disconnected:
        async def is_disconnected(self):
            return True
    closed = []
    async def operation():
        try:
            await anyio.sleep_forever()
        finally:
            closed.append(True)
    with pytest.raises(voice.HTTPException) as failure:
        anyio.run(voice._connected, Disconnected(), operation)
    assert failure.value.status_code == 499 and closed == [True]


def test_only_exact_internal_post_links_supply_narrative_evidence():
    text = " ".join(["https://evil.test/index.html?post=other%3ACode", "https://sentientdash.app.evil.test/?post=other%3ACode",
                     "https://sentientdash.app/index.html?post=rival%3AReal", "/index.html?post=second%3AItem",
                     "https://sentientdash.app/queue.html?post=third%3AQueue"])
    assert voice._references(text) == ["rival:Real", "second:Item"]


def test_stored_post_enrichment_is_read_only_scoped_and_bounded(monkeypatch):
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.execute("CREATE TABLE catalogue(account TEXT, shortcode TEXT, caption TEXT, hook_text TEXT, post_type_label TEXT, likes INTEGER, published_at TEXT)")
    raw.executemany("INSERT INTO catalogue VALUES (?,?,?,?,?,?,?)", [
        ("rival", "Real", "Caption " * 1000, "OCR " * 1000, "Reel", 10, "2026-11-06"),
        ("private", "Other", "Unrelated private content", "", "Image", 1, "2020-01-01")])
    raw.commit()
    raw.execute("PRAGMA query_only = ON")
    @contextmanager
    def connect():
        yield raw
    def sources(conn, handles, candidate_codes):
        assert handles == ["rival"] and candidate_codes == ["Real"]
        return "", [], {}
    monkeypatch.setattr(voice.db, "connect", connect)
    monkeypatch.setattr(voice.external_research, "_sources", sources)
    result = voice._stored_results(["rival:Real"])
    assert len(result) == 1 and len(result[0]["caption"]) == 1000 and len(result[0]["ocr_text"]) == 600
    assert "private" not in json.dumps(result)
    raw.close()


@pytest.mark.parametrize("role", ["pd", "sales", "trainee", "vc", "dev"])
def test_real_app_middleware_allows_voice_without_admin_or_read_marker(monkeypatch, role):
    from app import main
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": "member@example.test", "uid": "member"})
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: {
        "is_admin": False, "operating_role": role, "operating_roles": json.dumps([role]), "time_zone": "America/Costa_Rica"})
    monkeypatch.setattr(main, "user_flags", lambda *args: {})
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    voice._active.clear()
    voice._usage.clear()
    called = []
    async def transcribe(*args):
        called.append("transcribe")
        return "Latest post"
    async def synthesize(*args, **kwargs):
        called.append("speech")
        return b"RIFF-audio", "Existing answer"
    monkeypatch.setattr(voice, "_transcribe", transcribe)
    monkeypatch.setattr(voice, "_synthesize", synthesize)
    # Do not start background ingestion/startup maintenance in this integration
    # test; use the real mounted routes and complete middleware stack directly.
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer valid-member-token"}
    assert client.post("/api/dashboard/assistant/transcribe", headers=headers,
                       files={"file": ("audio.webm", WEBM, "audio/webm")}).status_code == 200
    assert client.post("/api/dashboard/assistant/speech", headers=headers, json={"text": "Existing answer"}).status_code == 200
    assert client.get("/api/dashboard/assistant/voices", headers=headers).status_code == 200
    assert client.post("/api/dashboard/assistant/voice-preview", headers=headers,
                       json={"voice": "coral", "language": "es", "sample": "results"}).status_code == 200
    assert called == ["transcribe", "speech", "speech"]
    assert client.post("/api/admin/users", headers=headers).status_code == 403


@pytest.mark.parametrize("token,expected", [(None, 401), ("invalid", 401), ("not-allowed", 403)])
def test_real_app_rejects_missing_invalid_or_revoked_identity_before_voice(monkeypatch, token, expected):
    from app import main
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    def verify(value):
        if value == "invalid":
            raise ValueError("expired")
        return {"email": value, "uid": "member"}
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", verify)
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: None)
    monkeypatch.setattr(voice, "_transcribe", lambda *args: pytest.fail("Authentication must reject before provider"))
    monkeypatch.setattr(voice, "_synthesize", lambda *args: pytest.fail("Authentication must reject before provider"))
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer " + token} if token else {}
    assert client.post("/api/dashboard/assistant/transcribe", headers=headers,
                       files={"file": ("audio.webm", WEBM, "audio/webm")}).status_code == expected
    assert client.post("/api/dashboard/assistant/speech", headers=headers, json={"text": "Existing answer"}).status_code == expected
    assert client.get("/api/dashboard/assistant/voices", headers=headers).status_code == expected
    assert client.post("/api/dashboard/assistant/voice-preview", headers=headers,
                       json={"voice": "coral", "language": "es", "sample": "results"}).status_code == expected
