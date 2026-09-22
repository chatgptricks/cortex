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
