import asyncio
from types import SimpleNamespace

import pytest

from app import dashboard_next
from app.dashboard_next import candidate_bundle

PATHS = ["/api/dashboard/accounts", "/api/insights/top-posts", "/api/dashboard/posts/{account}/{shortcode}/detail"]
ENTRIES = {str(i): {"path": path, "write": False} for i, path in enumerate(PATHS)}
ROSTER = [
    {"handle": "our.ai", "label": "Our AI", "group": "sentient", "subcategory": "ai_automation"},
    {"handle": "other.owned", "label": "Other Owned", "group": "sentient", "subcategory": "ai_automation"},
    {"handle": "competitor.ai", "label": "Competitor", "group": "competitors", "subcategory": "ai_automation"},
    {"handle": "competitor.news", "label": "News", "group": "competitors", "subcategory": "news_entertainment"},
    {"handle": "lead.ai", "label": "Lead", "group": "leads", "subcategory": "ai_automation"},
]


@pytest.fixture(autouse=True)
def history(monkeypatch):
    value = SimpleNamespace(post_count=150, examples=[{"caption": "Our editorial context"}],
                            filter_candidates=lambda rows: [{**row, "prior_posts": [{"key": "our.ai:Older", "caption": "Older context"}]} for row in rows])
    monkeypatch.setattr(dashboard_next.dashboard_next_reuse, "load_history", lambda target: value)
    return value


def harness(*, roster=None, empty_until=0, fail_path=None, incomplete=False, forged_account=None, ranking_rows=None):
    request = SimpleNamespace(headers={"authorization": "Bearer caller", "x-queue-role-preview": "pd"})
    calls = []
    accounts = ROSTER if roster is None else roster

    async def read(received, entry, parameters, analysis):
        assert received is request, "Reuse the caller's request, including authentication and role preview"
        assert entry["write"] is False
        path = entry["path"]
        calls.append((path, parameters, analysis))
        if path == fail_path:
            return {"status": 403, "error": "private backend error"}, {}
        source = {"endpoint": path, "url": "https://sentientdash.app/", "label": path}
        if path == PATHS[0]:
            assert analysis["filters"] == [{"field": "group", "op": "in", "value": ["sentient", "competitors"]}]
            data = {"data": accounts, "complete": True, "returned_rows": len(accounts), "result_rows": len(accounts)}
        elif path == PATHS[1]:
            query = parameters["query"]
            account = forged_account or query["account"].split(",")[0]
            data = {"data": [] if query["hours"] <= empty_until else [{"account": account, "shortcode": "Post123", "likes": 9000, "dashboard_url": "https://attacker.example/"}],
                    "coverage": {"complete": not incomplete}, "window": {"from": "start", "to": "end"}}
            if ranking_rows is not None:
                data["data"] = ranking_rows.get(query["hours"], [])
        elif path == PATHS[2]:
            data = {**parameters["path"], "caption": "caption " * 500, "ocrText": "cover " * 300}
        else:
            data = {"data": [{"account": "our.ai", "caption": "Target's current editorial topic"}], "pagination": {"has_more": True}}
        return {"status": 200, "complete": True, "truncated": False, "data": data}, source

    def run(account="our.ai", format="reel", *, excluded_keys=None, state=None):
        return asyncio.run(candidate_bundle(request, {"account": account, "format": format}, ENTRIES, read,
                                          excluded_keys=excluded_keys, state=state))
    return run, calls


@pytest.mark.parametrize("format", ["reel", "image", "carousel"])
def test_exact_format_and_verified_competitors_only(format):
    run, calls = harness()
    result = run(format=format)
    assert result["status"] == "ready"
    query = next(params["query"] for path, params, _ in calls if path == PATHS[1])
    assert query == {"account": "competitor.ai", "post_format": format, "hours": 24, "metric": "likes", "limit": 50}
    assert result["candidates"][0]["dashboard_url"] == "https://sentientdash.app/index.html?post=competitor.ai%3APost123"
    assert result["candidates"][0]["format"] == format
    assert len(result["candidates"][0]["caption"]) == 2400
    assert len(result["candidates"][0]["ocr_text"]) == 1200
    assert result["target_examples"] and result["history_complete"] is True
    assert not any("queue" in path for path, _, _ in calls)


@pytest.mark.parametrize("account", ["competitor.ai", "lead.ai", "made.up", "", "our"])
def test_invalid_destination_never_reads_posts(account):
    run, calls = harness()
    result = run(account=account)
    assert result["status"] == "invalid_target"
    assert result["available_targets"] == ["our.ai", "other.owned"]
    assert [path for path, _, _ in calls] == [PATHS[0]]


def test_exact_label_or_normalized_handle_and_ambiguous_label():
    assert harness()[0](account=" @OUR.AI ")["status"] == "ready"
    assert harness()[0](account="Our AI")["status"] == "ready"
    duplicate = {**ROSTER[1], "label": "Our AI"}
    run, calls = harness(roster=[ROSTER[0], duplicate, ROSTER[2]])
    assert run(account="Our AI")["status"] == "invalid_target"
    assert len(calls) == 1


def test_no_matching_subcategory_uses_only_other_competitors():
    run, calls = harness(roster=[ROSTER[0], ROSTER[1], ROSTER[3], ROSTER[4]])
    result = run()
    assert result["status"] == "ready"
    assert result["criteria"]["competitor_accounts"] == ["competitor.news"]
    assert result["criteria"]["same_subcategory"] is False


def test_expand_window_only_when_no_recent_candidates():
    run, calls = harness(empty_until=168)
    result = run()
    assert result["status"] == "ready"
    assert result["criteria"]["hours"] == 720
    assert [params["query"]["hours"] for path, params, _ in calls if path == PATHS[1]] == [24, 168, 720]


def test_no_candidates_is_empty_without_detail_reads():
    run, calls = harness(empty_until=720)
    assert run()["status"] == "empty"
    assert not any(path in PATHS[2:] for path, _, _ in calls)


def test_no_competitor_accounts_does_not_rank_owned_posts():
    run, calls = harness(roster=ROSTER[:2])
    assert run()["status"] == "empty"
    assert len(calls) == 1


@pytest.mark.parametrize("fail_path", PATHS)
def test_permission_errors_fail_closed_without_error_body(fail_path):
    run, calls = harness(fail_path=fail_path)
    result = run()
    assert result["status"] == "unavailable" and result["candidates"] == []
    assert "private backend error" not in str(result)
    assert calls[-1][0] == fail_path


def test_incomplete_ranking_or_unexpected_source_cannot_produce_recommendation():
    run, calls = harness(incomplete=True)
    assert run()["status"] == "unavailable"
    assert len(calls) == 2
    assert harness(forged_account="other.owned")[0]()["status"] == "unavailable"


def test_known_reused_candidates_are_excluded_and_search_expands(history):
    history.filter_candidates = lambda rows: []
    run, calls = harness()
    result = run()
    assert result["status"] == "empty" and result["reason"] == "no_unpublished_candidates"
    assert [params["query"]["hours"] for path, params, _ in calls if path == PATHS[1]] == [24, 168, 720]
    assert sum(path == PATHS[2] for path, _, _ in calls) == 1


def test_history_failure_cannot_fall_back_to_unchecked_recommendation(monkeypatch):
    def fail(_):
        raise ValueError("Cannot read target history")
    monkeypatch.setattr(dashboard_next.dashboard_next_reuse, "load_history", fail)
    run, calls = harness()
    assert run()["status"] == "unavailable"
    assert len(calls) == 1


def test_textless_unverifiable_candidate_is_not_recommended(history):
    history.filter_candidates = lambda rows: [{**row, "reuse_uncertain": True} for row in rows]
    assert harness()[0]()["status"] == "empty"


def test_semantic_rejections_continue_past_twelve_without_repeating_reads(history, monkeypatch):
    history_reads = []
    monkeypatch.setattr(dashboard_next.dashboard_next_reuse, "load_history", lambda target: history_reads.append(target) or history)
    rows = [{"account": "competitor.ai", "shortcode": f"Post{i:02}", "likes": 100 - i} for i in range(14)]
    run, calls = harness(ranking_rows={24: rows})
    state = {}
    first = run(state=state)
    assert len(first["candidates"]) == 12
    second = run(state=state, excluded_keys={row["key"] for row in first["candidates"]})
    assert [row["shortcode"] for row in second["candidates"]] == ["Post12", "Post13"]
    assert second["_history"] is first["_history"] is history
    assert history_reads == ["our.ai"]
    assert sum(path == PATHS[0] for path, _, _ in calls) == 1
    assert sum(path == PATHS[1] for path, _, _ in calls) == 1
    detail_codes = [params["path"]["shortcode"] for path, params, _ in calls if path == PATHS[2]]
    assert len(detail_codes) == len(set(detail_codes)) == 14


def test_semantic_rejections_expand_the_window_with_the_same_history(history):
    today = {"account": "competitor.ai", "shortcode": "Today", "likes": 200}
    week = {"account": "competitor.ai", "shortcode": "Week", "likes": 100}
    run, calls = harness(ranking_rows={24: [today], 168: [today, week]})
    state = {}
    first = run(state=state)
    second = run(state=state, excluded_keys={row["key"] for row in first["candidates"]})
    assert second["criteria"]["hours"] == 168
    assert [row["shortcode"] for row in second["candidates"]] == ["Week"]
    assert second["_history"] is history
    assert [params["query"]["hours"] for path, params, _ in calls if path == PATHS[1]] == [24, 168]
    assert [params["path"]["shortcode"] for path, params, _ in calls if path == PATHS[2]] == ["Today", "Week"]
