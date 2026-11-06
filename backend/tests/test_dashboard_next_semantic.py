import asyncio
import json
from types import SimpleNamespace

import pytest

from app.dashboard_next_semantic import retrieve_prior_posts


def test_multilingual_queries_retrieve_stored_evidence_without_becoming_evidence():
    received = []
    candidate = {"key": "peer:A", "caption": "Scientists discover a distant planet", "ocr_text": ""}
    def filter_candidates(rows):
        received.extend(rows)
        return [{**rows[0], "prior_posts": [{"key": "our:Old", "caption": "Científicos descubren un planeta lejano"}]}]
    async def call(*args, **kwargs):
        assert kwargs["max_output_tokens"] == 2400
        assert "caption" in json.dumps(args[3])
        return {"queries": [{"candidate_key": "peer:A", "search_text": "Scientists discover distant planet. Científicos descubren planeta lejano."}]}
    result = asyncio.run(retrieve_prior_posts(None, "key", {
        "history_complete": True, "history_posts_checked": 500,
        "_history": SimpleNamespace(filter_candidates=filter_candidates), "candidates": [candidate]}, call))
    assert "planeta" in received[0]["reuse_search_text"]
    assert "reuse_search_text" not in result[0]
    assert result[0]["caption"] == candidate["caption"]
    assert result[0]["prior_posts"][0]["key"] == "our:Old"


@pytest.mark.parametrize("queries", [[], [{"candidate_key": "invented", "search_text": "fake"}],
    [{"candidate_key": "peer:A", "search_text": ""}], [{"candidate_key": "peer:A", "search_text": "x" * 601}]])
def test_missing_or_invalid_search_preparation_fails_closed(queries):
    async def call(*args, **kwargs):
        return {"queries": queries}
    with pytest.raises(ValueError):
        asyncio.run(retrieve_prior_posts(None, "key", {
            "history_complete": True, "history_posts_checked": 1, "_history": object(),
            "candidates": [{"key": "peer:A"}]}, call))


def test_empty_verified_history_does_not_need_model_or_index():
    async def call(*args, **kwargs):
        raise AssertionError("No model is needed for an empty verified history")
    result = asyncio.run(retrieve_prior_posts(None, "key", {
        "history_complete": True, "history_posts_checked": 0, "candidates": [{"key": "peer:A"}]}, call))
    assert result == [{"key": "peer:A"}]
