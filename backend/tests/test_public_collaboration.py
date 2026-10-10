from __future__ import annotations

import json

import pytest

from app.public_collaboration import public_collaboration, stored_collaboration


@pytest.mark.parametrize(("source", "status", "participants"), [
    ({}, None, []),
    ({"coauthors": None}, None, []),
    ({"coauthors": ""}, None, []),
    ({"coauthors": []}, False, []),
    ({"coauthors": "[]"}, False, []),
    ({"coauthors": "  @PEER.Name, peer.name, OTHER_2  "}, True, ["peer.name", "other_2"]),
    ({"coauthors": '[{"username":"@PEER", "id":"CANARY"}, "PEER", "other"]'}, True, ["peer", "other"]),
    ({"raw_json": {"coauthorProducers": [{"username": "@PEER", "id": "CANARY"}, "peer"]}}, True, ["peer"]),
    ({"raw_json": json.dumps({"coauthorProducers": []})}, False, []),
    ({"raw_json": {"coauthorProducers": None}}, None, []),
    ({"raw_json": {"coauthorProducers": "peer"}}, None, []),
    ({"coauthors": {"username": "peer"}}, None, []),
    ({"coauthors": '["peer"'}, None, []),
    ({"coauthors": ["peer", {"id": "CANARY"}]}, True, ["peer"]),
    ({"coauthors": ["sample", {"id": "CANARY"}], "raw_json": {"ownerUsername": "sample"}}, None, []),
    ({"coauthors": ["sample", {"id": "CANARY"}], "raw_json": {"ownerUsername": "primary"}}, True, ["primary"]),
    ({"coauthors": ["https://instagram.com/peer"]}, None, []),
    ({"coauthors": ["a" * 31]}, None, []),
    ({"coauthors": ["KELVIN"]}, None, []),
    ({"coauthors": ["@aİ"]}, None, []),
    ({"coauthors": ["peer", "https://instagram.com/other", 123]}, True, ["peer"]),
    ({"coauthors": [123]}, None, []),
    ({"coauthors": ["sample"]}, None, []),
    ({"coauthors": ["sample"], "raw_json": {"ownerUsername": "@SAMPLE"}}, False, []),
    ({"raw_json": {"coauthorProducers": ["SAMPLE"], "ownerUsername": "owner"}}, True, ["owner"]),
    ({"raw_json": {"coauthorProducers": ["sample", "peer"], "owner": {"username": "OWNER"}}}, True, ["peer", "owner"]),
    ({"raw_json": {"coauthorProducers": ["sample", "peer"], "ownerUsername": "PEER"}}, True, ["peer"]),
    ({"raw_json": {"ownerUsername": "peer"}, "mentions": "peer", "tagged_users": "peer", "is_promo": True}, None, []),
    ({"coauthors": "old-peer", "raw_json": {"coauthorProducers": []}}, False, []),
    ({"coauthors": "peer", "raw_json": {"coauthorProducers": None}}, None, []),
])
def test_public_explicit_coauthor_shapes_and_unknowns(source, status, participants):
    assert public_collaboration(source, "@SAMPLE") == {"is_collab": status, "collaborators": participants}


@pytest.mark.parametrize("account", ["sample", "@SAMPLE"])
def test_stored_evidence_preserves_older_metadata_and_updates_explicit_empty(account):
    older = {"shortcode": "code", "updated_at": "2026-10-01T00:00:00Z", "_table": "posts",
             "coauthors": "peer"}
    latest = {"shortcode": "code", "updated_at": "2026-10-09T00:00:00Z", "_table": "dashboard_posts",
              "raw_json": {"likesCount": 99}}
    for missing in ({}, {"coauthors": None}, {"raw_json": {"coauthorProducers": [{"id": "unknown"}]}}):
        annotation = stored_collaboration([latest | missing, older], account)
        assert public_collaboration({"_public_collaboration": annotation}, account) == {"is_collab": True, "collaborators": ["peer"]}
    annotation = stored_collaboration([latest | {"raw_json": {"coauthorProducers": []}}, older], account)
    assert public_collaboration({"_public_collaboration": annotation}, account) == {"is_collab": False, "collaborators": []}


def test_metadata_timestamp_and_matching_observations_are_independent_of_metrics():
    records = [{"shortcode": "code", "updated_at": "2026-10-09T00:00:00Z", "enriched_at": "2026-10-01T00:00:00Z",
                "raw_json": {"coauthorProducers": []}},
               {"shortcode": "code", "updated_at": "2026-10-05T00:00:00Z", "coauthors": "peer"}]
    source = lambda observation: {"_public_collaboration": stored_collaboration(records, "sample", observation)}
    assert public_collaboration(source(None), "sample")["collaborators"] == ["peer"]
    for raw in ({"shortCode": "code", "likesCount": 999}, {"shortCode": "foreign", "coauthorProducers": []},
                {"shortcode": "foreign", "coauthorProducers": []},
                {"shortCode": "code", "coauthorProducers": None}):
        observation = {"observed_at": "2026-10-10T00:00:00Z", "raw_json": json.dumps(raw)}
        assert public_collaboration(source(observation), "sample")["is_collab"] is True
    observation = {"observed_at": "2026-10-10T00:00:00Z", "raw_json": {"shortCode": "code", "coauthorProducers": []}}
    assert public_collaboration(source(observation), "sample") == {"is_collab": False, "collaborators": []}


@pytest.mark.parametrize("invalid_date", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00", "malformed"])
def test_out_of_range_metadata_timestamp_never_throws_or_overrides_known_newer_evidence(invalid_date):
    records = [{"shortcode": "code", "coauthors": "peer", "updated_at": "2026-10-08T00:00:00Z"},
               {"shortcode": "code", "coauthors": [], "updated_at": invalid_date}]
    annotation = stored_collaboration(records, "sample")
    assert public_collaboration({"_public_collaboration": annotation}, "sample") == {"is_collab": True, "collaborators": ["peer"]}


def test_public_annotation_does_not_leak_or_reuse_other_account_evidence():
    annotation = stored_collaboration([{"coauthors": [{"username": "peer", "id": "CANARY", "profileUrl": "CANARY"}]}], "sample")
    public = public_collaboration({"_public_collaboration": annotation}, "sample")
    assert public == {"is_collab": True, "collaborators": ["peer"]}
    assert "CANARY" not in json.dumps(public) and "_account" not in public
    assert public_collaboration({"_public_collaboration": annotation}, "other") == {"is_collab": None, "collaborators": []}
