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
        queries = args[4]["parameters"]["properties"]["queries"]
        assert queries['required'] == ['peer:A']
        assert set(queries['properties']) == {'peer:A'} and queries['additionalProperties'] is False
        assert queries['properties']['peer:A']['pattern'] == r'^[\s\S]{1,240}$'
        return {"queries": {"peer:A": "Scientists discover distant planet. Científicos descubren planeta lejano."}}
    result = asyncio.run(retrieve_prior_posts(None, "key", {
        "history_complete": True, "history_posts_checked": 500,
        "_history": SimpleNamespace(filter_candidates=filter_candidates), "candidates": [candidate]}, call))
    assert "planeta" in received[0]["reuse_search_text"]
    assert "reuse_search_text" not in result[0]
    assert result[0]["caption"] == candidate["caption"]
    assert result[0]["prior_posts"][0]["key"] == "our:Old"


@pytest.mark.parametrize("queries,code", [
    ([], 'query_shape'), ({}, 'query_coverage'), ({'invented': 'fake'}, 'query_coverage'),
    ({'peer:A': ''}, 'query_text_empty'), ({'peer:A': '  \n'}, 'query_text_empty'),
    ({'peer:A': 3}, 'query_text_type'), ({'peer:A': 'x' * 241}, 'query_text_length'),
    ({'peer:A': 'valid', 'invented': 'extra'}, 'query_coverage'),
])
def test_missing_or_invalid_search_preparation_fails_closed_after_one_repair(queries, code):
    calls = []
    async def call(*args, **kwargs):
        calls.append((args, kwargs))
        return {"queries": queries}
    with pytest.raises(ValueError) as failure:
        asyncio.run(retrieve_prior_posts(None, "key", {
            "history_complete": True, "history_posts_checked": 1, "_history": object(),
            "candidates": [{"key": "peer:A"}]}, call))
    assert failure.value.code == code
    assert len(calls) == 2
    assert calls[0][0][3] == calls[1][0][3]  # The exact original evidence is reused.
    assert calls[0][0][4] == calls[1][0][4]
    assert calls[0][1] == calls[1][1] == {'max_output_tokens': 2400}
    assert 'SAME supplied evidence' in calls[1][0][2]


def test_repaired_search_uses_every_original_candidate_before_reuse_review():
    candidates = [{"key": "peer:A", "caption": "Lab announces Model 7"}, {"key": "peer:B", "caption": "Scientists discover distant planet"}]
    seen, calls = [], []
    def filter_candidates(rows):
        seen.extend(rows)
        return [{**row, 'prior_posts': [{'key': 'our:Old', 'caption': 'Stored historical evidence'}]} for row in rows]
    async def call(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return {'queries': {'peer:A': 'x' * 650}}  # Both incomplete coverage and overlong text.
        return {'queries': {'peer:A': 'Lab announces Model 7. Laboratorio anuncia Model 7.', 'peer:B': 'Scientists discover distant planet. Científicos descubren planeta lejano.'}}
    result = asyncio.run(retrieve_prior_posts(None, 'key', {
        'history_complete': True, 'history_posts_checked': 500,
        '_history': SimpleNamespace(filter_candidates=filter_candidates), 'candidates': candidates,
    }, call))
    assert len(calls) == 2 and calls[0][3] == calls[1][3]
    assert [row['key'] for row in seen] == ['peer:A', 'peer:B']
    assert [row['caption'] for row in result] == [row['caption'] for row in candidates]
    assert all('reuse_search_text' not in row for row in result)
    assert all(row['prior_posts'][0]['key'] == 'our:Old' for row in result)


def test_full_candidate_batch_has_exact_keys_and_compact_bilingual_output_budget():
    candidates = [{'key': f'peer:Post{i}', 'caption': f'Lab announces Model {i}', 'ocr_text': ''} for i in range(12)]
    async def call(*args, **kwargs):
        schema = args[4]['parameters']['properties']['queries']
        keys = [row['key'] for row in candidates]
        assert schema['required'] == keys and list(schema['properties']) == keys
        assert schema['additionalProperties'] is False
        assert kwargs['max_output_tokens'] == 2400
        return {'queries': {row['key']: f'Lab announces Model {i}. Laboratorio anuncia Model {i}.' for i, row in enumerate(candidates)}}
    result = asyncio.run(retrieve_prior_posts(None, 'key', {
        'history_complete': True, 'history_posts_checked': 500,
        '_history': SimpleNamespace(filter_candidates=lambda rows: rows), 'candidates': candidates,
    }, call))
    assert result == candidates


def test_repair_cannot_restore_a_reused_or_uncertain_candidate():
    candidates = [{'key': f'peer:{code}', 'caption': 'Original candidate facts'} for code in ('Reused', 'Uncertain', 'Review')]
    calls = []
    def filter_candidates(rows):
        assert [row['key'] for row in rows] == [row['key'] for row in candidates]
        return [{**rows[1], 'reuse_uncertain': True},
                {**rows[2], 'prior_posts': [{'key': 'our:Old', 'caption': 'Stored evidence for subsequent semantic review'}]}]
    async def call(*args, **kwargs):
        calls.append(1)
        return {'queries': {} if len(calls) == 1 else {row['key']: 'Candidate event. Evento del candidato.' for row in candidates}}
    result = asyncio.run(retrieve_prior_posts(None, 'key', {
        'history_complete': True, 'history_posts_checked': 500,
        '_history': SimpleNamespace(filter_candidates=filter_candidates), 'candidates': candidates,
    }, call))
    assert len(calls) == 2
    assert [row['key'] for row in result] == ['peer:Review']
    assert result[0]['prior_posts'][0]['key'] == 'our:Old'


def test_provider_budget_exhaustion_is_not_retried_again_by_query_repair():
    calls = []
    async def call(*args, **kwargs):
        calls.append(1)
        error = ValueError('Provider output exhausted its central retry.')
        error.code = 'provider_incomplete'
        raise error
    with pytest.raises(ValueError) as failure:
        asyncio.run(retrieve_prior_posts(None, 'key', {
            'history_complete': True, 'history_posts_checked': 1, '_history': object(),
            'candidates': [{'key': 'peer:A'}],
        }, call))
    assert calls == [1] and failure.value.code == 'provider_incomplete'


def test_invalid_json_arguments_have_a_sanitized_failure_code():
    async def call(*args, **kwargs):
        raise ValueError('private provider payload must not be exposed')
    with pytest.raises(ValueError) as failure:
        asyncio.run(retrieve_prior_posts(None, 'key', {
            'history_complete': True, 'history_posts_checked': 1, '_history': object(),
            'candidates': [{'key': 'peer:A'}],
        }, call))
    assert failure.value.code == 'query_arguments'
    assert 'private' not in str(failure.value)


def test_empty_verified_history_does_not_need_model_or_index():
    async def call(*args, **kwargs):
        raise AssertionError("No model is needed for an empty verified history")
    result = asyncio.run(retrieve_prior_posts(None, "key", {
        "history_complete": True, "history_posts_checked": 0, "candidates": [{"key": "peer:A"}]}, call))
    assert result == [{"key": "peer:A"}]
