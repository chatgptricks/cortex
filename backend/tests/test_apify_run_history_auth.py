from types import SimpleNamespace

import httpx
import pytest

from app import main


def test_run_history_authentication_failure_does_not_expose_the_token(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "synthetic-private-token")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler)
    ))
    request = SimpleNamespace(state=SimpleNamespace(is_dev=True))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        main.temp_runs(request, limit=3)

    assert requests[0].headers["authorization"] == "Bearer synthetic-private-token"
    assert requests[0].url.params == httpx.QueryParams({"limit": 3, "desc": "true"})
    assert "synthetic-private-token" not in str(caught.value)
