from __future__ import annotations

from copy import deepcopy

import pytest

from app.apify_sync import ApifySyncError, _extract_import_run_items, _filter_items_for_account


def ivan_profile(children):
    # Exact public identity/field shape of the already-paid details run:
    # parent username/id; latestPosts ownerUsername/ownerId are strings.
    return {"username": "ivanelgrande", "id": "1301481635", "fullName": "Ivan",
            "inputUrl": "https://www.instagram.com/ivanelgrande/",
            "url": "https://www.instagram.com/ivanelgrande", "latestPosts": children}


def ivan_post(code="DdukxwLEp6m", **overrides):
    return {"shortCode": code, "ownerUsername": "ivanelgrande", "ownerId": "1301481635",
            "timestamp": "2026-09-28T15:00:00Z", "type": "Sidecar", "productType": "carousel_container",
            "likesCount": 117288, "commentsCount": 1839, "videoViewCount": 500,
            "caption": "Actual published caption", "url": f"https://www.instagram.com/p/{code}/",
            "displayUrl": "https://cdn.example.test/cover.jpg", "images": [], "videoUrl": None,
            "childPosts": [{"shortCode": "slideA", "ownerUsername": "ivanelgrande",
                            "ownerId": "1301481635", "type": "Image"}], **overrides}


def test_paid_profile_extracts_public_parent_posts_and_preserves_real_metrics_without_slides():
    parent = ivan_post()
    dataset = [ivan_profile([parent, ivan_post("second", type="Video", productType="clips")])]
    original = deepcopy(dataset)
    result = _extract_import_run_items(dataset, "@IvanElGrande")
    assert dataset == original
    assert [item["shortCode"] for item in result] == ["DdukxwLEp6m", "second"]
    assert result[0]["likesCount"] == 117288
    assert result[0]["commentsCount"] == 1839
    assert result[0]["videoViewCount"] == 500
    assert result[0]["caption"] == "Actual published caption"
    assert result[0]["childPosts"] == parent["childPosts"]
    filtered, foreign = _filter_items_for_account(result, "ivanelgrande")
    assert filtered == result and foreign == []


def test_ownerless_and_matching_id_only_children_inherit_only_the_verified_profile_identity():
    ownerless = {"shortCode": "ownerless", "likesCount": 50}
    id_only = {"shortCode": "id-only", "ownerId": 1301481635}
    nested = {"shortCode": "nested", "owner": {"id": "1301481635", "username": "IVANELGRANDE"}}
    result = _extract_import_run_items([ivan_profile([ownerless, id_only, nested])], "ivanelgrande")
    assert [item["shortCode"] for item in result] == ["ownerless", "id-only", "nested"]
    assert all(item["ownerUsername"] == "ivanelgrande" for item in result)
    assert result[0]["ownerId"] == "1301481635"
    assert "ownerUsername" not in ownerless


@pytest.mark.parametrize("overrides", [
    {"ownerUsername": "someone_else"},
    {"owner": {"username": "someone_else"}},
    {"username": "someone_else"},
    {"ownerId": "99999"},
    {"owner": {"id": "99999"}},
    {"ownerUsername": {"unattributed": "value"}},
    {"owner": "someone_else"},
    {"ownerId": True},
])
def test_contradictory_or_malformed_child_owner_is_never_relabelled(overrides):
    dataset = [ivan_profile([ivan_post("foreign", **overrides), ivan_post("valid")])]
    result = _extract_import_run_items(dataset, "ivanelgrande")
    assert [item["shortCode"] for item in result] == ["valid"]


@pytest.mark.parametrize("overrides", [
    {"username": "someone_else"},
    {"username": None},
    {"username": "ivanelgrande", "ownerUsername": "someone_else"},
    {"username": "ivanelgrande", "owner": {"username": "someone_else"}},
    {"username": "ivanelgrande", "ownerId": "99999"},
])
def test_foreign_or_unattributed_profile_dataset_is_rejected_even_if_children_name_the_target(overrides):
    profile = {**ivan_profile([ivan_post()]), **overrides}
    with pytest.raises(ApifySyncError, match="no profile explicitly matching"):
        _extract_import_run_items([profile], "ivanelgrande")


def test_profile_id_is_not_confused_with_the_child_media_id():
    # A parent post's own media ID is distinct from its ownerId.
    result = _extract_import_run_items([ivan_profile([ivan_post(id="183838338999")])], "ivanelgrande")
    assert result[0]["id"] == "183838338999"
    assert result[0]["ownerId"] == "1301481635"


def test_id_only_child_without_known_profile_id_is_not_attributed_from_an_input_url():
    profile = ivan_profile([{"shortCode": "unknown-id", "ownerId": "1301481635"},
                            {"shortCode": "named", "ownerUsername": "ivanelgrande"},
                            {"shortCode": "ownerless"}])
    profile.pop("id")
    result = _extract_import_run_items([profile], "ivanelgrande")
    assert [item["shortCode"] for item in result] == ["named", "ownerless"]


def test_mixed_flat_and_profile_items_are_deduped_and_the_existing_owner_filter_remains_in_force():
    dataset = [
        {"shortCode": "flat", "ownerUsername": "ivanelgrande", "likesCount": 10},
        {"shortcode": "ownerless", "likesCount": 20},
        {"shortCode": "foreign", "ownerUsername": "someone_else"},
        ivan_profile([ivan_post("flat"), ivan_post("nested")]),
    ]
    result = _extract_import_run_items(dataset, "ivanelgrande")
    assert [item["shortCode"] for item in result] == ["flat", "ownerless", "nested", "foreign"]
    filtered, foreign = _filter_items_for_account(result, "ivanelgrande")
    assert [item["shortCode"] for item in filtered] == ["flat", "ownerless", "nested"]
    assert foreign == ["someone_else"]


def test_foreign_flat_duplicate_cannot_mask_a_matching_nested_parent_post():
    result = _extract_import_run_items([
        {"shortCode": "same", "ownerUsername": "someone_else", "likesCount": 1},
        ivan_profile([ivan_post("same")]),
    ], "ivanelgrande")
    filtered, foreign = _filter_items_for_account(result, "ivanelgrande")
    assert len(filtered) == 1 and filtered[0]["likesCount"] == 117288
    assert foreign == ["someone_else"]


def test_flat_url_only_posts_and_all_foreign_flat_filter_behavior_are_preserved():
    result = _extract_import_run_items([{"url": "https://www.instagram.com/reel/urlOnly/"}], "ivanelgrande")
    assert result[0]["shortCode"] == "urlOnly"
    assert _filter_items_for_account(result, "ivanelgrande") == (result, [])
    result = _extract_import_run_items([{"shortCode": "foreign", "ownerUsername": "someone_else"}], "ivanelgrande")
    with pytest.raises(ApifySyncError, match="Dataset belongs to"):
        _filter_items_for_account(result, "ivanelgrande")


def test_extractor_never_recurses_into_related_profiles_child_posts_or_nested_containers():
    dataset = [{"relatedProfiles": [ivan_profile([ivan_post("related")])]},
               {"childPosts": [ivan_post("slide")]},
               {"data": {"latestPosts": [ivan_post("nested-data")]}},
               ivan_profile([None, "malformed", {"childPosts": [ivan_post("nested-slide")]}])]
    assert _extract_import_run_items(dataset, "ivanelgrande") == []


def test_nonmatching_profiles_are_ignored_when_an_explicit_target_profile_is_present():
    dataset = [{**ivan_profile([ivan_post("foreign-wrapper")]), "username": "someone_else"},
               ivan_profile([ivan_post("target")])]
    result = _extract_import_run_items(dataset, "ivanelgrande")
    assert [item["shortCode"] for item in result] == ["target"]
