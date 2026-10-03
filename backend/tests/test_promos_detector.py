import pytest

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


def test_tool_credit_and_availability_alone_do_not_prove_promotion():
    result = detect_promo({"caption": "Built with @higgsfield — available now."})
    assert result["classification"] == "not_promo"
    assert result["client"] == "higgsfield"


def test_narrative_send_information_is_not_an_automation_cta():
    result = detect_promo({"caption": "and send information back to human operators"})
    assert result["cta"] is None
    assert result["classification"] == "not_promo"


def test_narrative_replies_and_mentions_are_not_a_promo():
    result = detect_promo({"caption": "Send replies—all with ChatGPT on your Mac. Now available in ChatGPT Work."})
    assert result["cta"] is None
    assert result["classification"] == "not_promo"


def test_generic_cta_without_commercial_relationship_is_not_a_promo():
    result = detect_promo({"caption": "Comment below and tell us what you think."})
    assert result["classification"] == "not_promo"


def test_editorial_code_phrase_is_not_a_promo_code():
    result = detect_promo({"caption": "The code and model are publicly available."})
    assert result["promo_code"] is None
    assert result["classification"] == "not_promo"


@pytest.mark.parametrize("caption", [
    "OpenAI launched an advertisement platform today.",
    "The company sponsored a robotics study, according to the report.",
    "OpenAI announced a paid partnership with @microsoft today.",
    'The headline says "sponsored by @acme" in a report about advertising.',
    "Not an advertisement, just my independent review of @higgsfield.",
    "Not a paid partnership. I independently tested @higgsfield.",
    "No es publicidad. Estoy probando @higgsfield.",
    "Built with @higgsfield for my animation class.",
    "Try making this effect. Built with @higgsfield.",
])
def test_editorial_negated_and_tool_credit_counterexamples(caption):
    assert detect_promo({"caption": caption})["classification"] == "not_promo"


@pytest.mark.parametrize("caption,brand", [
    ("Credit to @creator. Sponsored by @higgsfield. Try Higgsfield Video.", "higgsfield"),
    ("Thanks to @creator for editing. Sponsored by @higgsfield.", "higgsfield"),
    ("Sponsored by Acme. Try Acme Pro. #ad", "Acme"),
    ("Credit to @creator. #HiggsfieldSponsored", "Higgsfield"),
    ("This article is sponsored by @acme.", "acme"),
    ("News about AI. This post is sponsored by @acme.", "acme"),
])
def test_relationship_client_beats_incidental_mentions(caption, brand):
    result = detect_promo({"caption": caption})
    assert result["classification"] == "disclosed"
    assert result["client"] == brand


@pytest.mark.parametrize("caption", [
    "Built with @higgsfield. Try Higgsfield today.",
    "Built with @higgsfield. Sign up for a free trial.",
    "Built with @higgsfield. Use code SAVE20 at https://higgsfield.ai.",
])
def test_tool_credit_with_offer_can_be_likely(caption):
    assert detect_promo({"caption": caption})["classification"] == "likely"


@pytest.mark.parametrize("caption", [
    "Comment below and tell us what you think.",
    "DM me for details.",
    "Send us your feedback.",
    "Comenta abajo para participar.",
])
def test_generic_cta_prose_has_no_automation_keyword(caption):
    assert detect_promo({"caption": caption})["cta"] is None


@pytest.mark.parametrize("caption,keyword", [
    ("DM me VIDEO for the link.", "VIDEO"),
    ('Comment "AI" to get the download.', "AI"),
    ("Send us VIDEO for access.", "VIDEO"),
    ('Comenta la palabra "VIDEO".', "VIDEO"),
])
def test_intentional_cta_keyword_skips_pronouns(caption, keyword):
    assert detect_promo({"caption": caption})["cta"]["keyword"] == keyword


def test_url_punctuation_is_not_part_of_client_or_link():
    result = detect_promo({"caption": "Use code SAVE20 at https://brand.example.", "first_comment": "https://brand.example/offer!"})
    assert result["client"] == "brand.example"
    assert result["links"] == [{"url": "https://brand.example", "source": "caption"}, {"url": "https://brand.example/offer", "source": "first_comment"}]


def test_conflicting_disclosures_are_reviewable_and_affiliate_is_independent():
    conflict = detect_promo({"caption": "Not sponsored, just testing @brand #brandpartner"})
    assert conflict["classification"] == "needs_review"
    assert any(item["family"] == "negation" for item in conflict["evidence"])
    offer = detect_promo({"caption": "Not sponsored. Use code SAVE20 at https://brand.example."})
    assert offer["classification"] == "likely"
