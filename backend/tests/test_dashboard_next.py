import asyncio
from types import SimpleNamespace

import pytest

from app.dashboard_next import candidate_bundle

PATHS = ["/api/dashboard/accounts", "/api/insights/top-posts", "/api/dashboard/posts/{account}/{shortcode}/detail", "/api/dashboard/posts/search"]
ENTRIES = {str(i): {"path": path, "write": False} for i, path in enumerate(PATHS)}
ROSTER = [
    {"handle": "our.ai", "label": "Our AI", "group": "sentient", "subcategory": "ai_automation"},
    {"handle": "other.owned", "label": "Other Owned", "group": "sentient", "subcategory": "ai_automation"},
    {"handle": "competitor.ai", "label": "Competitor", "group": "competitors", "subcategory": "ai_automation"},
    {"handle": "competitor.news", "label": "News", "group": "competitors", "subcategory": "news_entertainment"},
    {"handle": "lead.ai", "label": "Lead", "group": "leads", "subcategory": "ai_automation"},
]


def harness(*, roster=None, empty_until=0, fail_path=None, incomplete=False, forged_account=None):
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
        elif path == PATHS[2]:
            data = {**parameters["path"], "caption": "caption " * 500, "ocrText": "cover " * 300}
        else:
            data = {"data": [{"account": "our.ai", "caption": "Target's current editorial topic"}], "pagination": {"has_more": True}}
        return {"status": 200, "complete": path != PATHS[3], "truncated": False, "data": data}, source

    def run(account="our.ai", format="reel"):
        return asyncio.run(candidate_bundle(request, {"account": account, "format": format}, ENTRIES, read))
    return run, calls


@pytest.mark.parametrize("format", ["reel", "image", "carousel"])
def test_exact_format_and_verified_competitors_only(format):
    run, calls = harness()
    result = run(format=format)
    assert result["status"] == "ready"
    query = next(params["query"] for path, params, _ in calls if path == PATHS[1])
    assert query == {"account": "competitor.ai", "post_format": format, "hours": 24, "metric": "likes", "limit": 12}
    assert result["candidates"][0]["dashboard_url"] == "https://sentientdash.app/index.html?post=competitor.ai%3APost123"
    assert result["candidates"][0]["format"] == format
    assert len(result["candidates"][0]["caption"]) == 1200
    assert len(result["candidates"][0]["ocr_text"]) == 500
    assert result["target_examples"] and result["reuse_checked"] is False
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
