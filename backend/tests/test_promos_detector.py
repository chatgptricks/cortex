from app.promos_detector import detect_promo


def test_explicit_sponsorship_extracts_mention_and_cta():
    result = detect_promo({"caption": "Sponsored by @higgsfield. Try Higgsfield Video. Comment VIDEO for the link"})
    assert result["classification"] == "disclosed"
    assert result["client"] == "higgsfield"
    assert result["cta"]["keyword"] == "VIDEO"


def test_compound_partner_hashtag_is_reviewable():
    result = detect_promo({"caption": "#lovablepartner — try this workflow"})
    assert result["is_promo"]
    assert result["client"] == "lovable"
    assert result["classification"] == "disclosed"


def test_ad_substring_does_not_false_positive():
    result = detect_promo({"caption": "made this #adventure, download the guide"})
    assert result["classification"] == "not_promo"


def test_negated_disclosure_is_not_confirmed():
    result = detect_promo({"caption": "not sponsored, just testing X"})
    assert result["classification"] == "not_promo"


def test_paid_metadata_without_caption_is_disclosed_unknown():
    result = detect_promo({"caption": "", "paid_partnership": 1})
    assert result["classification"] == "disclosed"
    assert result["client"] is None
    assert result["product"] is None
