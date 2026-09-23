from app import jev_features


def _answer_set(score=4, confidence=0.9, nugget=0.9, account="chatgptricks", novelty=None):
    answers = {
        key: {"type": "score", "score": score, "confidence": confidence}
        for key in (
            "insight",
            "audience_value",
            "hook",
            "repurpose",
            "evergreen",
            "concreteness",
            "distinctiveness",
        )
    }
    answers["golden_nugget"] = {"type": "noul", "noul": nugget}
    answers["best_account"] = {"type": "choice", "choice": account, "confidence": confidence}
    if novelty is not None:
        answers["novelty"] = {"type": "score", "score": novelty, "confidence": confidence}
    return answers


def test_golden_nugget_requires_many_strong_signals(monkeypatch):
    monkeypatch.setattr(jev_features, "ask_jev", lambda state, questions: _answer_set())

    result = jev_features.golden_nugget_review(
        "A specific, evidence-backed idea with a rare angle.",
        "source_account",
        [{"handle": "chatgptricks", "label": "ChatGPT Tricks"}],
    )

    assert result["label"] == "golden_nugget"
    assert result["targetAccount"] == "chatgptricks"
    assert result["strongSignalCount"] == 7


def test_golden_nugget_rejects_weak_or_unowned_ideas(monkeypatch):
    monkeypatch.setattr(
        jev_features,
        "ask_jev",
        lambda state, questions: _answer_set(score=3, confidence=0.9, nugget=0.35, account="none"),
    )

    result = jev_features.golden_nugget_review(
        "A generic trend observation with no clear owner.",
        "source_account",
        [{"handle": "chatgptricks", "label": "ChatGPT Tricks"}],
    )

    assert result["label"] == "not_yet"
    assert result["targetAccount"] is None


def test_news_novelty_is_required_for_golden_nugget(monkeypatch):
    monkeypatch.setattr(jev_features, "ask_jev", lambda state, questions: _answer_set(novelty=2))

    result = jev_features.golden_nugget_review(
        "A strong idea that repeats a post already in the dashboard.",
        "news-source",
        [{"handle": "chatgptricks", "label": "ChatGPT Tricks"}],
        novelty_context=[{"account": "chatgptricks", "shortcode": "abc", "text": "Existing post"}],
    )

    assert result["label"] == "potential"
    assert result["novelty"]["isNewAngle"] is False


def test_news_without_comparisons_does_not_claim_verified_novelty(monkeypatch):
    monkeypatch.setattr(jev_features, "ask_jev", lambda state, questions: _answer_set(novelty=4))

    result = jev_features.golden_nugget_review(
        "A rare, specific idea not previously covered.",
        "news-source",
        [{"handle": "chatgptricks", "label": "ChatGPT Tricks"}],
        novelty_context=[],
    )

    assert result["label"] == "golden_nugget"
    assert result["novelty"] is None


def test_news_good_candidate_passes_calibrated_top_candidate_threshold(monkeypatch):
    monkeypatch.setattr(jev_features, "ask_jev", lambda state, questions: _answer_set(score=3, novelty=3))

    result = jev_features.golden_nugget_review(
        "A useful and meaningfully new story with a clear adaptation path.",
        "news-source",
        [{"handle": "chatgptricks", "label": "ChatGPT Tricks"}],
        novelty_context=[],
    )

    assert result["label"] == "golden_nugget"
    assert result["score"] == 0.75
