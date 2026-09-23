"""Bounded Jev judgments used by the dashboard's semantic tools.

Jev supplies typed judgments. This module keeps network calls and thresholds in
one place; the API layer remains responsible for permissions and mutations.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class JevFeatureUnavailable(RuntimeError):
    """A semantic feature cannot return a trustworthy result right now."""


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _noul(answers: dict[str, Any], key: str) -> float:
    answer = answers.get(key) or {}
    if isinstance(answer, dict):
        return _clamp(answer.get("noul"))
    return _clamp(answer)


def _score_answer(answers: dict[str, Any], key: str, levels: int = 5) -> tuple[float, float]:
    """Return a normalized Score value and its confidence."""
    answer = answers.get(key) or {}
    if not isinstance(answer, dict):
        return 0.0, 0.0
    try:
        score = float(answer.get("score"))
    except (TypeError, ValueError):
        return 0.0, 0.0
    try:
        confidence = _clamp(answer.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(1.0, score / max(1, levels - 1))), confidence


def _choice_answer(answers: dict[str, Any], key: str) -> tuple[str, float]:
    answer = answers.get(key) or {}
    if not isinstance(answer, dict):
        return "none", 0.0
    return str(answer.get("choice") or "none"), _clamp(answer.get("confidence"))


def ask_jev(state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
    """Run one bounded fan-out request and fail closed on missing answers."""
    api_key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        raise JevFeatureUnavailable("This Jev feature requires TYPESAFE_API_KEY.")
    try:
        import httpx

        response = httpx.post(
            "https://api.typesafe.ai/v1/systemone",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "jev-latest", "state": state, "questions": questions},
            timeout=35.0,
        )
        response.raise_for_status()
        payload = response.json()
        answers = payload.get("answers") or {}
        if not isinstance(answers, dict) or any(key not in answers for key in questions):
            raise JevFeatureUnavailable("Jev returned an incomplete response.")
        return answers
    except JevFeatureUnavailable:
        raise
    except Exception as exc:
        logger.warning("Jev semantic feature failed: %s", exc)
        raise JevFeatureUnavailable("Jev is temporarily unavailable.") from exc


def verify_caption(source_caption: str, candidate_caption: str, target_account: str) -> dict[str, Any]:
    questions = {
        "fact_fidelity": {
            "type": "noul",
            "instructions": "Does the candidate caption preserve the source's supported facts and core subject without inventing claims?",
            "criteria": {
                "true": "The candidate keeps the same supported facts and does not add unsupported claims.",
                "false": "The candidate changes a material fact, invents a claim, or loses the source's core subject.",
            },
        },
        "target_alignment": {
            "type": "noul",
            "instructions": "Is the candidate caption correctly adapted for the selected target account?",
            "criteria": {
                "true": "The caption speaks for the target account and does not promote a different account.",
                "false": "It addresses, credits, promotes, or directs users to the wrong account.",
            },
        },
        "unsupported_claims": {
            "type": "noul",
            "instructions": "Does the candidate add any material fact, number, quote, date, link, or claim not supported by the source?",
            "criteria": {
                "true": "At least one material unsupported addition is present.",
                "false": "There are no material unsupported additions.",
            },
        },
    }
    answers = ask_jev(
        {
            "source_caption": source_caption[:6000],
            "candidate_caption": candidate_caption[:12000],
            "target_account": f"@{target_account.lstrip('@')}",
        },
        questions,
    )
    fidelity = _noul(answers, "fact_fidelity")
    target = _noul(answers, "target_alignment")
    unsupported = _noul(answers, "unsupported_claims")
    return {
        "factFidelity": fidelity,
        "targetAlignment": target,
        "unsupportedClaims": unsupported,
        "accepted": fidelity >= 0.78 and target >= 0.82 and unsupported <= 0.22,
        "mode": "jev_verification",
    }


def classify_post(text: str) -> dict[str, Any]:
    labels = {
        "news": "breaking news or a reported event",
        "analysis": "analysis, explanation, or industry interpretation",
        "announcement": "a product, company, person, or feature announcement",
        "tutorial": "instructions, tips, or educational guidance",
        "promo": "a commercial promotion, sponsorship, or product CTA",
        "opinion": "a personal viewpoint or editorial take",
        "other": "none of the categories above",
    }
    questions: dict[str, Any] = {}
    for key, description in labels.items():
        questions[key] = {
            "type": "noul",
            "instructions": f"Is this post primarily {description}? Choose true only when this is the dominant intent.",
            "criteria": {"true": f"The dominant intent is {description}.", "false": "Another intent is dominant or the evidence is insufficient."},
        }
    answers = ask_jev({"post_text": text[:7000]}, questions)
    scores = {key: _noul(answers, key) for key in labels}
    label = max(scores, key=scores.get)
    return {"label": label, "scores": scores, "mode": "jev_classification"}


def golden_nugget_review(
    text: str,
    source_account: str = "",
    target_accounts: list[dict[str, str]] | None = None,
    novelty_context: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Judge whether a post contains reusable editorial value, independent of heat."""
    dimensions = {
        "insight": (0.24, "How strong is the useful insight or idea in this post?", [
            "No reusable idea", "A vague observation", "A useful but familiar idea",
            "A clear, meaningful insight", "A sharp, unusually valuable insight",
        ]),
        "audience_value": (0.20, "How much would this help or matter to our target audience?", [
            "No clear audience value", "Narrow or weak value", "Useful to a defined audience",
            "Broadly useful to our audience", "Highly useful and likely to earn saves or shares",
        ]),
        "hook": (0.16, "How much hook potential does the underlying idea have for a new post?", [
            "No viable hook", "Needs a completely new idea", "One workable hook is visible",
            "Several strong hooks are visible", "The idea naturally creates a compelling hook",
        ]),
        "repurpose": (0.18, "How flexibly can the idea be adapted into content for our accounts?", [
            "Cannot be adapted without copying", "Adaptation would be forced", "One plausible adaptation",
            "Several natural formats or angles", "Highly adaptable across accounts and formats",
        ]),
        "evergreen": (0.12, "How durable is the idea beyond the post's current moment?", [
            "Only useful for the immediate moment", "Expires very quickly", "Useful for a short window",
            "Mostly evergreen", "Durable and useful well beyond the original post",
        ]),
        "concreteness": (0.10, "How much concrete evidence, detail, or specificity supports the idea?", [
            "Pure assertion with no useful detail", "Very little detail", "Some supporting detail",
            "Concrete details make it credible", "Specific evidence makes it highly defensible",
        ]),
        "distinctiveness": (0.10, "How rare or differentiated is the underlying idea in our content landscape?", [
            "Generic or already common", "Slightly different wording only", "A somewhat distinct angle",
            "Clearly differentiated from common takes", "Rare, surprising, and difficult to substitute",
        ]),
    }
    target_accounts = target_accounts or []
    account_criteria = {
        str(item.get("handle") or "none"): (
            f"{item.get('label') or item.get('handle')}: active Sentient account. Recent content: {item.get('examples') or 'No examples available; infer fit conservatively from the account name.'}"
        )
        for item in target_accounts
        if str(item.get("handle") or "").strip()
    }
    account_criteria["none"] = "No active Sentient account has a credible, natural fit for this idea."
    questions: dict[str, Any] = {}
    for key, (_, instruction, levels) in dimensions.items():
        questions[key] = {
            "type": "score",
            "instructions": (
                f"{instruction} Evaluate the underlying editorial opportunity, not the post's likes, "
                "views, account size, or current virality. A post can be a golden nugget even when it is not Hot."
            ),
            "criteria": levels,
        }
    questions["golden_nugget"] = {
        "type": "noul",
        "instructions": (
            "Does this post contain a specific, defensible idea worth developing into an original post "
            "for one of our accounts, even if the source post has low engagement?"
        ),
        "criteria": {
            "true": "There is a clear reusable insight with a credible path to an original adaptation.",
            "false": "The post is mostly noise, generic commentary, unsupported claims, or dependent on its current hype.",
        },
    }
    questions["best_account"] = {
        "type": "choice",
        "instructions": (
            "Which active Sentient account is the most natural owner for an original post based on this idea? "
            "Consider an accessible original adaptation for the account audience, not copying the source style. Recent posts are examples, not topic restrictions. Choose none only when the idea is outside every account audience."
        ),
        "criteria": account_criteria,
    }
    if novelty_context is not None:
        questions["editorial_angle"] = {
            "type": "choice",
            "instructions": "Choose the strongest original editorial treatment supported by the supplied source excerpt. Do not assume facts missing from the excerpt.",
            "criteria": {
                "practical_guide": "Teach a useful workflow, tool or concrete action the reader can try.",
                "comparison": "Explain a supported difference between tools, approaches, costs or capabilities.",
                "what_changes": "Explain a specific announcement and its practical consequences for the reader.",
                "visual_explainer": "Make a technical concept or robotics development easy to understand visually.",
                "needs_reporting": "The excerpt lacks enough substance; investigate the original source before developing a post.",
            },
        }
        questions["post_format"] = {
            "type": "choice",
            "instructions": "Which post format best communicates the supported idea?",
            "criteria": {
                "carousel": "An ordered explanation with several distinct useful points.",
                "reel": "A demonstration or visual story, subject to obtaining usable footage.",
                "single_post": "One clear announcement, takeaway or comparison fits one image and caption.",
            },
        }
    if bool(novelty_context):
        questions["novelty"] = {
            "type": "score",
            "instructions": (
                "How new is the underlying editorial idea compared with the existing dashboard posts provided in "
                "the state? Judge the idea and angle, not exact wording. A topic can be familiar while the angle is "
                "new. Penalize ideas that substantially repeat an existing post."
            ),
            "criteria": [
                "Already covered by an existing dashboard post",
                "Mostly the same idea with minor wording changes",
                "Somewhat new but overlaps an existing angle",
                "Clearly new angle not covered by the existing posts",
                "Rare, differentiated opportunity with no meaningful dashboard precedent",
            ],
        }
    answers = ask_jev(
        {
            "source_account": f"@{source_account.lstrip('@')}",
            "post_text": text[:9000],
            "evaluation_rule": "Do not use engagement or Hot status as evidence of editorial value.",
            "active_sentient_accounts": account_criteria,
            "existing_dashboard_posts": novelty_context or [],
        },
        questions,
    )
    scores: dict[str, float] = {}
    confidences: dict[str, float] = {}
    for key in dimensions:
        scores[key], confidences[key] = _score_answer(answers, key)
    total_weight = sum(weight for weight, _, _ in dimensions.values())
    weighted_score = sum(scores[key] * weight for key, (weight, _, _) in dimensions.items()) / total_weight
    novelty_score = 0.0
    novelty_confidence = 0.0
    if bool(novelty_context):
        novelty_score, novelty_confidence = _score_answer(answers, "novelty")
        # Novelty is a meaningful multiplier, not a cosmetic badge. It gets
        # 18% of the final score while preserving the original signal mix.
        weighted_score = weighted_score * 0.82 + novelty_score * 0.18
    confidence_values = list(confidences.values())
    if bool(novelty_context):
        confidence_values.append(novelty_confidence)
    confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
    jev_signal = _noul(answers, "golden_nugget")
    best_account, account_confidence = _choice_answer(answers, "best_account")
    critical_floor = min(scores.get(key, 0.0) for key in ("insight", "audience_value", "hook", "repurpose", "distinctiveness"))
    strong_signal_count = sum(value >= 0.68 for value in scores.values())
    if bool(novelty_context) and novelty_score >= 0.68:
        strong_signal_count += 1
    novelty_gate = not novelty_context or novelty_score >= 0.68
    # News discovery judges the idea separately from assigning its eventual owner.
    account_gate = novelty_context is not None or (best_account != "none" and account_confidence >= 0.55)
    if (
        weighted_score >= 0.72
        and jev_signal >= 0.65
        and account_gate
        and critical_floor >= 0.50
        and strong_signal_count >= 4
        and novelty_gate
    ):
        label = "golden_nugget"
    # Potential is a deliberately wider discovery bucket for ideas that have
    # evidence and an adaptation path but miss one or more Golden gates.
    elif (
        weighted_score >= 0.56
        and jev_signal >= 0.40
        and critical_floor >= 0.30
        and strong_signal_count >= 2
        and (novelty_context is not None or account_gate)
    ):
        label = "potential"
    else:
        label = "not_yet"
    ranked_dimensions = sorted(scores, key=scores.get, reverse=True)
    return {
        "label": label,
        "score": round(weighted_score, 4),
        "confidence": round(confidence, 4),
        "jevSignal": round(jev_signal, 4),
        "targetAccount": best_account if best_account != "none" else None,
        "targetAccountConfidence": round(account_confidence, 4),
        "strongSignalCount": strong_signal_count,
        "classificationGates": {
            "golden": {"score": 0.72, "jevSignal": 0.65, "criticalDimensionFloor": 0.50, "strongSignals": 4, "novelty": 0.68},
            "potential": {"score": 0.56, "jevSignal": 0.40, "criticalDimensionFloor": 0.30, "strongSignals": 2},
        },
        "dimensions": {key: {"score": round(scores[key], 4), "confidence": round(confidences[key], 4)} for key in dimensions},
        "strengths": ranked_dimensions[:3],
        "weaknesses": ranked_dimensions[-2:],
        "novelty": {
            "score": round(novelty_score, 4),
            "confidence": round(novelty_confidence, 4),
            "isNewAngle": bool(novelty_context) and novelty_score >= 0.68,
        } if bool(novelty_context) else None,
        "editorialAngle": _choice_answer(answers, "editorial_angle")[0] if novelty_context is not None else None,
        "postFormat": _choice_answer(answers, "post_format")[0] if novelty_context is not None else None,
        "mode": "jev_golden_nugget",
    }


def queue_suggestions(text: str) -> dict[str, Any]:
    tags = ["content", "design", "copy", "research", "review", "repurpose"]
    questions: dict[str, Any] = {
        "needs_queue": {
            "type": "noul",
            "instructions": "Would this post benefit from a production Queue task in the current editorial workflow?",
            "criteria": {"true": "It needs a concrete production, research, copy, design, or review action.", "false": "It is informational only or needs no production action."},
        },
        "urgent": {
            "type": "noul",
            "instructions": "Does this post have a time-sensitive reason to receive urgent Queue priority?",
            "criteria": {"true": "Delay would materially reduce its editorial value.", "false": "It can be handled through the normal workflow."},
        },
    }
    for tag in tags:
        questions[f"tag_{tag}"] = {
            "type": "noul",
            "instructions": f"Is `{tag}` the single best Queue tag for the work this post needs?",
            "criteria": {"true": f"The primary work is {tag}.", "false": "Another tag better describes the primary work."},
        }
    answers = ask_jev({"post_text": text[:7000]}, questions)
    tag_scores = {tag: _noul(answers, f"tag_{tag}") for tag in tags}
    return {
        "needsQueue": _noul(answers, "needs_queue") >= 0.65,
        "urgent": _noul(answers, "urgent") >= 0.80,
        "tag": max(tag_scores, key=tag_scores.get),
        "tagScores": tag_scores,
        "mode": "jev_queue_suggestion",
    }


def review_promo(text: str, deterministic: dict[str, Any]) -> dict[str, Any]:
    questions = {
        "semantic_promo": {
            "type": "noul",
            "instructions": "Does this post semantically promote a product, service, brand relationship, affiliate offer, or commercial CTA?",
            "criteria": {"true": "The post has commercial intent even if the exact keywords are absent.", "false": "It is editorial or informational without commercial intent."},
        },
        "needs_review": {
            "type": "noul",
            "instructions": "Is the promotion classification ambiguous enough that a human should review it?",
            "criteria": {"true": "Evidence is mixed, indirect, or could reasonably be interpreted either way.", "false": "The commercial status is clear from the available evidence."},
        },
    }
    answers = ask_jev({"post_text": text[:7000], "deterministic_analysis": deterministic}, questions)
    semantic = _noul(answers, "semantic_promo")
    review = _noul(answers, "needs_review")
    return {
        "semanticPromo": semantic,
        "needsReview": review >= 0.55,
        "deterministicClassification": deterministic.get("classification"),
        "mode": "jev_promo_review",
    }


def audit_stack(reference_text: str, members: dict[str, str]) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    for index, (post_key, text) in enumerate(members.items(), start=1):
        questions[f"member_{index}"] = {
            "type": "noul",
            "instructions": f"Does member `{post_key}` belong in the same narrowly defined Topic Stack as the reference?",
            "criteria": {"true": "Same underlying event, claim, release, product, or person.", "false": "Only broad subject overlap or materially different story."},
        }
    answers = ask_jev({"reference_text": reference_text[:6000], "members": {key: value[:3000] for key, value in members.items()}}, questions)
    results = []
    for index, post_key in enumerate(members, start=1):
        score = _noul(answers, f"member_{index}")
        results.append({"postKey": post_key, "sameStack": score >= 0.72, "score": score})
    return {"members": results, "mode": "jev_stack_audit"}


def rank_search(query: str, candidates: dict[str, str]) -> list[dict[str, Any]]:
    questions = {}
    for index, post_key in enumerate(candidates, start=1):
        questions[f"candidate_{index}"] = {
            "type": "noul",
            "instructions": f"Is `{post_key}` directly relevant to the user's research query, not merely in the same broad category?",
            "criteria": {"true": "It answers, illustrates, or materially informs the query.", "false": "It is only loosely related or irrelevant."},
        }
    answers = ask_jev({"query": query[:1000], "candidates": {key: value[:2500] for key, value in candidates.items()}}, questions)
    ranked = [
        {"postKey": post_key, "score": _noul(answers, f"candidate_{index}")}
        for index, post_key in enumerate(candidates, start=1)
    ]
    return sorted(ranked, key=lambda item: (-item["score"], item["postKey"]))
