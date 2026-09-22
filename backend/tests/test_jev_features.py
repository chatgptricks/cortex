from app import jev_features


def _answer_set(score=4, confidence=0.9, nugget=0.9, account="chatgptricks"):
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
