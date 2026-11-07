"""Bounded /next alternatives keep the requested count and reuse evidence."""
import asyncio
import json

import anyio
import httpx
import pytest
from fastapi import HTTPException

from app import dashboard_assistant as assistant
from test_dashboard_next_flow import (app, candidate_stub, function_call, prepared_history_evidence,  # noqa: F401
                                      provider_mock, ready_bundle, run)


def bundle_with(count, start=0, *, history=False):
    bundle = ready_bundle()
    base = bundle["candidates"][0]
    bundle["post_format"] = "carousel"
    bundle["history_posts_checked"] = 100 if history else 0
    bundle["candidates"] = [{**base, "key": f"evolving.ai:P{i}", "shortcode": f"P{i}", "likes": 10000 - i,
        "caption": f"Specific new story {i}", "prior_posts": [{"key": "chatgptricks:Old", "caption": "An older story"}],
        "dashboard_url": assistant.dashboard_next.dashboard_post_url("evolving.ai", f"P{i}")} for i in range(start, start + count)]
    return bundle


def prepared(count=5, post_format="carousel", target="chatgptricks"):
    return {"post_format": post_format, "target_account": target, "option_count": count}


def offered_choices(payload, *, count=None):
    schema = payload["tools"][0]["parameters"]["properties"]["choices"]
    keys = schema["items"]["properties"]["candidate_key"]["enum"]
    expected = schema["minItems"]
    assert expected == schema["maxItems"]
    return {"choices": [{"candidate_key": key, "reason": "A relevant recent story with observed engagement."}
                        for key in keys[:expected if count is None else count]]}


def test_exact_five_options_query_checks_every_candidate_and_accumulates_batches(app, monkeypatch):
    batches, state_ids, reviewed = [], [], []
    all_rows = bundle_with(10, history=True)["candidates"]
    vetoed = {"evolving.ai:P0", "evolving.ai:P1", "evolving.ai:P5", "evolving.ai:P6"}

    async def candidates(req, slots, entries, read, *, excluded_keys, state):
        assert slots == prepared()
        batches.append(set(excluded_keys))
        state_ids.append(id(state))
        assert len(batches) <= 2, "Enough verified options must stop further reads"
        return {**bundle_with(0, history=True), "candidates": [row for row in all_rows if row["key"] not in excluded_keys][:5]}

    monkeypatch.setattr(assistant.dashboard_next, "candidate_bundle", candidates)

    def answer(payload, turn):
        name = payload["tool_choice"]["name"]
        if name == "prepare_next_recommendation":
            assert payload["input"][0]["content"] == "carousel for chatgptricks, provide 5 options"
            assert payload["tools"][0]["parameters"]["properties"]["option_count"]["maximum"] == 10
            return [function_call(name, prepared())]
        if name == "check_next_publication_history":
            rows = json.loads(payload["input"][0]["content"])["candidates"]
            reviewed.extend(row["key"] for row in rows)
            return [function_call(name, {"checks": [{"candidate_key": row["key"],
                "status": "already_posted" if row["key"] in vetoed else "new",
                "matched_post_key": "chatgptricks:Old" if row["key"] in vetoed else None} for row in rows]})]
        assert name == "choose_next_post" and turn == 4
        evidence = json.loads(payload["input"][1]["content"].split("=", 1)[1])
        assert len(evidence["candidates"]) == 6
        assert all(row["key"] not in vetoed and "prior_posts" not in row for row in evidence["candidates"])
        return [function_call(name, offered_choices(payload))]

    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="/next carousel for chatgptricks, provide 5 options", language="en")
    assert not result.get("error_code") and result["ui_actions"] == []
    assert result["answer"].count("https://sentientdash.app/") == 5
    assert all(f"evolving.ai%3AP{i}" in result["answer"] for i in (2, 3, 4, 7, 8))
    assert all(f"evolving.ai%3AP{i}" not in result["answer"] for i in (0, 1, 5, 6))
    assert "9,998 likes" in result["answer"] and "requested options" not in result["answer"]
    assert len(calls) == 4 and len(reviewed) == len(set(reviewed)) == 10
    assert batches == [set(), {row["key"] for row in all_rows[:5]}] and len(set(state_ids)) == 1
    assert "enum" not in assistant.NEXT_CHOOSE_TOOL["parameters"]["properties"]["choices"]["items"]["properties"]["candidate_key"]


def test_option_count_survives_format_and_destination_clarifications(app, monkeypatch):
    candidate_calls = candidate_stub(monkeypatch, bundle_with(5))

    def answer(payload, _):
        name = payload["tool_choice"]["name"]
        if name == "choose_next_post":
            return [function_call(name, offered_choices(payload))]
        pending = json.loads(payload["instructions"].split("\nPENDING_NEXT=", 1)[1])
        message = payload["input"][0]["content"]
        if message == "provide five options":
            assert pending["option_count"] == 1
            values = prepared(post_format=None, target=None)
        elif message == "carousel":
            assert pending == prepared(post_format=None, target=None)
            values = prepared(target=None)
        else:
            assert message == "chatgptricks" and pending == prepared(target=None)
            values = prepared()
        return [function_call(name, values)]

    calls = provider_mock(monkeypatch, answer)
    first = run(app, message="/next provide five options", language="en")
    assert first["pending_next"] == prepared(post_format=None, target=None) and not candidate_calls
    second = run(app, message="carousel", next_context=first["pending_next"], language="en")
    assert second["pending_next"] == prepared(target=None) and not candidate_calls
    final = run(app, message="chatgptricks", next_context=second["pending_next"], language="en")
    assert final["answer"].count("https://sentientdash.app/") == 5
    assert len(candidate_calls) == 1 and len(calls) == 4


def test_invalid_target_keeps_requested_option_count(app, monkeypatch):
    candidate_stub(monkeypatch, {"status": "invalid_target", "available_targets": ["chatgptricks"]})
    provider_mock(monkeypatch, lambda *_: [function_call("prepare_next_recommendation", prepared(target="competitor"))])
    result = run(app, message="/next five carousels for competitor")
    assert result["pending_next"] == prepared(target=None)


def test_fresh_next_does_not_inherit_an_older_option_count(app, monkeypatch):
    candidate_calls = candidate_stub(monkeypatch, bundle_with(5))
    def answer(payload, _):
        pending = json.loads(payload["instructions"].split("\nPENDING_NEXT=", 1)[1])
        assert pending == prepared(count=1, post_format=None, target=None)
        return [function_call("prepare_next_recommendation", prepared(count=1, post_format="image", target=None))]
    provider_mock(monkeypatch, answer)
    result = run(app, message="/next image", next_context=prepared())
    assert result["pending_next"] == prepared(count=1, post_format="image", target=None) and not candidate_calls


@pytest.mark.parametrize("count", [1, 10])
def test_supported_option_count_boundaries_select_exact_number(app, monkeypatch, count):
    candidate_stub(monkeypatch, bundle_with(12))
    def answer(payload, _):
        name = payload["tool_choice"]["name"]
        return [function_call(name, prepared(count=count) if name == "prepare_next_recommendation" else offered_choices(payload))]
    calls = provider_mock(monkeypatch, answer)
    result = run(app, message=f"/next carousel for chatgptricks, {count} options", language="en")
    assert len(calls) == 2 and not result.get("error_code")
    assert result["answer"].count("https://sentientdash.app/") == count


@pytest.mark.parametrize("count", [0, 11, True, 2.5, "5"])
def test_invalid_prepared_count_cannot_start_search(app, monkeypatch, count):
    candidates = candidate_stub(monkeypatch, bundle_with(5))
    provider_mock(monkeypatch, lambda *_: [function_call("prepare_next_recommendation", prepared(count=count))])
    result = run(app, message="/next carousel for chatgptricks")
    assert not candidates and result["error_code"] == "next_prepare_unavailable"
    assert "https://" not in result["answer"]


@pytest.mark.parametrize("failure", ["empty", "unavailable", "history_error", "provider_rate_limit", "review_timeout"])
def test_later_batch_failure_returns_only_previously_verified_subset(app, monkeypatch, failure):
    batches = []
    monkeypatch.setattr(assistant, "MAX_NEXT_REVIEW_SECONDS", .1 if failure == "review_timeout" else 80)

    async def candidates(req, slots, entries, read, *, excluded_keys, state):
        batches.append(set(excluded_keys))
        if len(batches) == 1:
            return bundle_with(3)
        if failure == "review_timeout":
            await anyio.sleep_forever()
        if failure == "provider_rate_limit":
            raise HTTPException(429, "Provider is busy")
        if failure == "history_error":
            return bundle_with(2, start=3)
        return {"status": failure}

    async def history(client, key, bundle, tool):
        if bundle["candidates"][0]["shortcode"] == "P3":
            raise ValueError("Unverified next batch")
        return bundle["candidates"]

    monkeypatch.setattr(assistant.dashboard_next, "candidate_bundle", candidates)
    monkeypatch.setattr(assistant.dashboard_next_semantic, "retrieve_prior_posts", history)

    def answer(payload, _):
        name = payload["tool_choice"]["name"]
        return [function_call(name, prepared() if name == "prepare_next_recommendation" else offered_choices(payload))]

    provider_mock(monkeypatch, answer)
    result = run(app, message="/next carousel for chatgptricks, provide 5 options", language="en")
    assert not result.get("error_code")
    assert result["answer"].count("https://sentientdash.app/") == 3
    assert "I could verify 3 of the 5 requested options." in result["answer"]
    assert "evolving.ai%3AP3" not in result["answer"]
    assert len(batches) == 2 and len(batches[1]) == 3


def test_batch_budget_returns_verified_subset_without_a_fifth_batch(app, monkeypatch):
    batches = []
    async def candidates(req, slots, entries, read, *, excluded_keys, state):
        batches.append(set(excluded_keys))
        return bundle_with(1, start=len(batches) - 1)
    monkeypatch.setattr(assistant.dashboard_next, "candidate_bundle", candidates)
    def answer(payload, _):
        name = payload["tool_choice"]["name"]
        return [function_call(name, prepared() if name == "prepare_next_recommendation" else offered_choices(payload))]
    provider_mock(monkeypatch, answer)
    result = run(app, message="/next carousel for chatgptricks, cinco opciones", language="es")
    assert len(batches) == assistant.MAX_NEXT_REUSE_BATCHES == 4
    assert result["answer"].count("https://sentientdash.app/") == 4
    assert "Pude verificar 4 de las 5 opciones solicitadas." in result["answer"]


@pytest.mark.parametrize("invalid", ["too_few", "duplicate", "invented"])
@pytest.mark.parametrize("repair", [True, False])
def test_selection_must_return_exact_unique_verified_count(app, monkeypatch, invalid, repair):
    candidate_stub(monkeypatch, bundle_with(6))
    def answer(payload, turn):
        name = payload["tool_choice"]["name"]
        if turn == 1:
            return [function_call(name, prepared())]
        values = offered_choices(payload)
        if turn == 2 or not repair:
            if invalid == "too_few":
                values["choices"].pop()
            elif invalid == "duplicate":
                values["choices"][-1] = values["choices"][0]
            else:
                values["choices"][-1]["candidate_key"] = "invented:MADE_UP"
        return [function_call(name, values)]
    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="/next carousel for chatgptricks, provide 5 options", language="en")
    assert len(calls) == 3
    if repair:
        assert not result.get("error_code") and result["answer"].count("https://sentientdash.app/") == 5
    else:
        assert result["error_code"] == "next_selection_unavailable" and "https://" not in result["answer"]
    assert "MADE_UP" not in result["answer"]


@pytest.mark.parametrize("failure", ["provider_incomplete", "provider_invalid_tool"])
def test_provider_selection_failure_does_not_restart_exhausted_retry_budget(app, monkeypatch, failure):
    candidate_stub(monkeypatch, bundle_with(5))
    original = httpx.AsyncClient
    calls = []

    def provider(req):
        payload = json.loads(req.content)
        name = payload["tool_choice"]["name"]
        calls.append(name)
        if name == "prepare_next_recommendation":
            return httpx.Response(200, json={"output": [function_call(name, prepared())]})
        if failure == "provider_incomplete":
            assert len(calls) <= 3
            return httpx.Response(200, json={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []})
        assert len(calls) == 2
        return httpx.Response(200, json={"output": [function_call("unoffered", {})]})

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = run(app, message="/next carousel for chatgptricks, provide 5 options", language="en")
    if failure == "provider_incomplete":
        assert len(calls) == 3 and result["error_code"] == "next_selection_provider_incomplete"
        assert "https://" not in result["answer"]
        assert "selecting options" in result["answer"] and "Dashboard data" not in result["answer"]
    else:
        assert len(calls) == 2 and result["error_code"] == "next_selection_provider_invalid_tool"
        assert "https://" not in result["answer"]


def test_selection_timeout_cannot_bypass_additional_user_preferences(app, monkeypatch):
    candidate_stub(monkeypatch, bundle_with(5))
    monkeypatch.setattr(assistant, "MAX_NEXT_SELECTION_SECONDS", .01)
    async def blocked(*args, **kwargs):
        await anyio.sleep_forever()
    monkeypatch.setattr(assistant, "choose_next_options", blocked)
    provider_mock(monkeypatch, lambda *_: [function_call("prepare_next_recommendation", prepared())])
    result = run(app, message="/next carousel for chatgptricks, provide 5 options about robotics ONLY", language="en")
    assert result["error_code"] == "next_selection_timeout" and "https://" not in result["answer"]
    assert "selecting options" in result["answer"] and "Dashboard data" not in result["answer"]
