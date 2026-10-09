from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import apify_sync, main


@pytest.fixture
def recovery_route(monkeypatch):
    state = {"calls": [], "inserted": [], "items": [], "existing": [{"shortcode": "alreadySaved"}]}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            state["calls"].append(("GET", url))
            payload = {"data": {"status": "SUCCEEDED", "defaultDatasetId": "paidDataset"}} if "actor-runs" in url else deepcopy(state["items"])
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: state["existing"]))

    def insert(handle, cfg, items):
        assert handle == cfg["handle"] == "ivanelgrande"
        state["inserted"].extend(deepcopy(items))
        return {"added": len(items), "failed": 0}

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: Client())
    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(apify_sync, "get_account_config", lambda handle: {"handle": handle, "table": "dashboard_posts", "is_canonical": False})
    monkeypatch.setattr(apify_sync, "_insert_new_posts", insert)
    monkeypatch.setattr(apify_sync, "_run_apify_actor_and_fetch", lambda *args, **kwargs: pytest.fail("Recovery must never start a paid run"))
    request = Request({"type": "http", "headers": []})
    request.state.is_dev = True
    return request, state


def test_completed_profile_dataset_recovers_nested_public_parent_posts_without_paid_start(recovery_route):
    request, state = recovery_route
    state["items"] = [{"username": "ivanelgrande", "id": "ownerId", "latestPosts": [
        {"shortCode": "winner", "ownerUsername": "ivanelgrande", "ownerId": "ownerId", "timestamp": "2026-09-28T12:00:00Z", "likesCount": 117288,
         "childPosts": [{"shortCode": "slideMustNotBecomePost"}]},
        {"shortCode": "alreadySaved", "ownerUsername": "ivanelgrande", "timestamp": "2026-09-10T12:00:00Z"},
        {"shortCode": "olderNew", "ownerUsername": "ivanelgrande", "timestamp": "2026-09-18T12:00:00Z"},
        {"shortCode": "winner", "ownerUsername": "ivanelgrande", "timestamp": "2026-09-28T12:00:00Z", "likesCount": 117288},
        {"shortCode": "foreign", "ownerUsername": "someoneElse"},
    ]}]
    result = main.temp_import_run("ivanelgrande", "paidRun", request)
    assert result["run_status"] == "SUCCEEDED"
    assert result["new"] == result["result"]["added"] == 2
    assert [post["shortCode"] for post in state["inserted"]] == ["olderNew", "winner"]
    assert state["inserted"][1]["likesCount"] == 117288
    assert state["calls"] == [("GET", "https://api.apify.com/v2/actor-runs/paidRun"),
                              ("GET", "https://api.apify.com/v2/datasets/paidDataset/items")]


def test_foreign_profile_dataset_is_rejected_before_any_insert(recovery_route):
    request, state = recovery_route
    state["items"] = [{"username": "someoneElse", "latestPosts": [{"shortCode": "foreign"}]}]
    with pytest.raises(HTTPException) as error:
        main.temp_import_run("ivanelgrande", "paidRun", request)
    assert error.value.status_code == 409
    assert not state["inserted"]


def test_flat_completed_post_dataset_keeps_existing_recovery_behavior(recovery_route):
    request, state = recovery_route
    state["items"] = [{"shortCode": "alreadySaved", "ownerUsername": "ivanelgrande"},
                      {"shortCode": "newFlat", "ownerUsername": "ivanelgrande", "likesCount": 42}]
    result = main.temp_import_run("ivanelgrande", "paidRun", request)
    assert result["new"] == 1
    assert state["inserted"][0]["shortCode"] == "newFlat"


def test_profile_recovery_retains_admin_or_dev_authorization_before_provider_read(recovery_route):
    request, state = recovery_route
    request.state.is_dev = False
    with pytest.raises(HTTPException) as error:
        main.temp_import_run("ivanelgrande", "paidRun", request)
    assert error.value.status_code == 403
    assert state["calls"] == state["inserted"] == []
