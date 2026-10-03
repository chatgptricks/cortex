"""Deterministic, explainable promotion detection for competitor posts."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit
from typing import Any

DETECTOR_VERSION = "promos-2"

_EXPLICIT = [
    ("sponsored", r"\bsponsored(?:\s+by)?\b"),
    ("paid partnership", r"\bpaid\s+(?:partnership|promotion|collaboration)\b"),
    ("advertisement", r"\badvertisement\b|\bpublicidad\b"),
    ("paid", r"\bpaid\s+(?:ad|content|placement)\b|\bpatrocinad[oa]\b"),
    ("ad hashtag", r"(?<![\w])#(?:ad|sponsored|publicidad|patrocinado)(?![\w])"),
    ("collaboration paid", r"\bcolaboraci[oó]n\s+(?:pagada|de\s+pago)\b"),
]
_RELATION = [
    ("partner", r"\bpartner(?:ed|ship)?\b|\bin\s+partnership\s+with\b|\bbrand\s+ambassador\b"),
    ("collab", r"\bcollab(?:oration)?\b|\bcolaboraci[oó]n\b"),
    ("gifted", r"\bgifted\b|\bproducto\s+recibido\b|\bthanks\s+to\b"),
]
_AFFILIATE = [
    ("affiliate", r"\baffiliate(?:\s+link)?\b|\benlace\s+de\s+afiliado\b"),
    ("commission", r"\bearn\s+(?:a\s+)?commission\b|\bcomisi[oó]n\b"),
    ("promo code", r"\b(?:discount|promo|referral)\s+code\b|\bc[oó]digo\s+de\s+descuento\b"),
    ("use code", r"\buse\s+(?:my\s+)?code\b|\busa\s+mi\s+c[oó]digo\b"),
]
_CTA = [
    ("link in bio", r"\blink\s+in\s+bio\b|\benlace\s+en\s+bio\b"),
    ("try", r"\b(?:try|check out|get access|sign up|prueba|reg[ií]strate)\b"),
]
_COMMERCIAL = [
    ("availability", r"\b(?:available|launching|now live|download|start using)\b"),
    ("built with", r"\b(?:built|made|created)\s+(?:with|using)\b"),
]
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.I)
_HASHTAG_RE = re.compile(r"(?<![\w])#([\wÀ-ÿ-]+)", re.UNICODE)
_MENTION_RE = re.compile(r"(?<![\w])@([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)")
_CTA_KEYWORD_RE = re.compile(r"\b(comment|comenta|reply|responde|dm|env[ií]a|send)\s+(?:(?:me|us|the word|la palabra)\s+)?[\"'“”]?([A-Za-z0-9][A-Za-z0-9_-]{1,32})", re.I)
_CODE_RE = re.compile(r"\b(?:use\s+(?:my\s+)?code|(?:discount|promo|referral)\s+code|c[oó]digo\s+de\s+descuento)\s*[:#-]?\s*([A-Za-z0-9_-]{3,32})", re.I)
_GENERIC_CTA_WORDS = {"information", "info", "details", "detail", "data", "message", "messages", "link", "more", "questions", "replies", "reply", "response", "responses", "answer", "answers", "and", "in", "to", "back", "operators", "human", "below", "here", "now", "your", "you", "us", "me", "with", "for", "the", "a", "an", "it", "this", "that", "them", "about", "abajo", "aqui", "aquí", "tu", "tus", "con", "por", "para"}
_BRAND_RELATION_RE = re.compile(
    r"\b(?:sponsored\s+by|paid\s+(?:partnership|collaboration)\s+with|"
    r"in\s+partnership\s+with|partnered\s+with|partner(?:ship)?\s+(?:with\s+)?|"
    r"patrocinad[oa]\s+por|thanks\s+to)\s*"
    r"(?P<brand>@[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*|[A-Z][\w-]*(?:\s+(?-i:[A-Z])[\w-]*){0,2})",
    re.I,
)


def _extract_cta_keyword(caption: str) -> tuple[str, str] | None:
    """Extract an intentional comment/DM keyword, excluding narrative prose."""
    for match in _CTA_KEYWORD_RE.finditer(caption):
        action, candidate = match.group(1), match.group(2)
        if candidate.casefold() in _GENERIC_CTA_WORDS:
            continue
        # "send information back/to ..." describes data flow, not an audience CTA.
        if action.casefold() == "send" and re.match(r"\s+(?:back|to|toward)\b", caption[match.end():], re.I):
            continue
        return action, candidate
    return None


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = "".join(ch for ch in value if unicodedata.category(ch) not in {"Cf", "Cc"} or ch in "\n\t")
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 65):start]
    return bool(re.search(r"\b(?:not|never|no|sin|isn't|isn’t|wasn't|wasn’t)\s+(?:(?:a|an|any|es|una?|paid|financially)\s+){0,3}$", prefix, re.I))


def _is_editorial_disclosure(text: str, start: int, end: int) -> bool:
    """Disclosures describe this post; reporting about advertising does not."""
    before = re.split(r"[.!?;]\s+|\n", text[:start])[-1][-90:]
    after = text[end:end + 100]
    signal = text[start:end]
    if re.match(r"\s+(?:platform|industry|market|policy|policies|revenue|business|ban|regulation|technology|format|formats)\b", after):
        return True
    if re.search(r"\b(?:report(?:s|ed|ing)?|article|news|headline|announced|study|research|according to)\b", before) and not re.search(r"\b(?:this|our|my)\s+(?:post|video|content|episode|article|review|tutorial)\b", before):
        return True
    if signal == "sponsored" and not re.match(r"\s+by\b", after):
        # "Acme sponsored a study/event" reports somebody else's activity.
        if re.match(r"\s+(?:a|an|the|their)\s+", after):
            return True
    if re.search(r"[\"“']\s*$", before) and re.search(r"[\"”']", after):
        return True
    return False


def _urls(text: str) -> list[str]:
    return [url.rstrip(".,;:!?…，。！？”’") for url in _URL_RE.findall(text)]


def _evidence(family: str, name: str, text: str, source: str = "caption") -> dict[str, str]:
    return {"family": family, "rule": name, "source": source, "text": text[:240]}


def _candidate_names(caption: str, hashtags: list[str], mentions: list[str], urls: list[str]) -> list[dict[str, str]]:
    names: list[dict[str, str]] = []
    seen: set[str] = set()
    # The sponsor named in the relationship beats an incidental creator credit.
    relations = sorted(_BRAND_RELATION_RE.finditer(caption), key=lambda match: 0 if re.match(r"sponsored|paid|patrocinad", match.group(0), re.I) else 2 if match.group(0).casefold().startswith("thanks") else 1)
    for match in relations:
        normalized = caption.casefold()
        if _is_negated(normalized, match.start()) or _is_editorial_disclosure(normalized, match.start(), match.start("brand")):
            continue
        value = match.group("brand").lstrip("@").rstrip(".,")
        if value.casefold() in {"a", "an", "the", "this", "our", "my", "your", "us", "me"}:
            continue
        key = value.casefold()
        if key not in seen:
            names.append({"name": value, "source": "relationship", "evidence": match.group(0)})
            seen.add(key)
    for tag in hashtags:
        clean = tag.strip("_-")
        lowered = clean.casefold()
        for suffix in ("sponsored", "partner", "partnership", "collab", "affiliate", "ad"):
            if lowered.endswith(suffix) and len(clean) > len(suffix) + 2:
                candidate = clean[: -len(suffix)].strip("_-")
                if candidate and candidate.casefold() not in seen:
                    names.append({"name": candidate, "source": "hashtag"})
                    seen.add(candidate.casefold())
    for value in mentions:
        value = value.lstrip("@").rstrip(".,")
        key = value.casefold()
        if key not in seen:
            names.append({"name": value, "source": "mention"})
            seen.add(key)
    for url in urls:
        host = urlsplit(url).hostname or ""
        host = re.sub(r"^(?:www|m)\.", "", host, flags=re.I)
        if host and host.casefold() not in seen and host not in {"instagram.com", "tiktok.com", "youtube.com"}:
            names.append({"name": host, "source": "url"})
            seen.add(host.casefold())
    return names[:12]


def _has_offer(text: str, candidates: list[dict[str, str]]) -> bool:
    if re.search(r"\b(?:link in bio|enlace en bio|sign up|get access|buy|shop|purchase|subscribe|start (?:your |a )?free trial|reg[ií]strate)\b", text, re.I):
        return True
    for candidate in candidates:
        name = re.escape(candidate["name"])
        if re.search(rf"\b(?:try|check out|prueba)\s+@?{name}(?![\w])", text, re.I):
            return True
    return False


def detect_promo(post: dict[str, Any]) -> dict[str, Any]:
    caption = str(post.get("caption") or "")
    first_comment = str(post.get("first_comment") or "")
    hashtags = [str(x) for x in (post.get("hashtags") or [])] if isinstance(post.get("hashtags"), list) else [x.strip() for x in str(post.get("hashtags") or "").split(",") if x.strip()]
    mentions = [str(x) for x in (post.get("mentions") or [])] if isinstance(post.get("mentions"), list) else [x.strip() for x in str(post.get("mentions") or "").split(",") if x.strip()]
    caption_urls = _urls(caption)
    urls = caption_urls + _urls(first_comment)
    text = _normalize(caption)
    evidence: list[dict[str, str]] = []
    explicit = False
    negated_explicit = False
    for name, pattern in _EXPLICIT:
        for match in re.finditer(pattern, text, re.I):
            snippet = caption[max(0, match.start() - 45): match.end() + 90]
            negated = _is_negated(text, match.start())
            editorial = _is_editorial_disclosure(text, match.start(), match.end())
            evidence.append(_evidence("negation" if negated else "editorial" if editorial else "explicit", name, snippet))
            explicit = explicit or (not negated and not editorial)
            negated_explicit = negated_explicit or negated
    for family, rules in (("relationship", _RELATION), ("affiliate", _AFFILIATE), ("cta", _CTA), ("commercial", _COMMERCIAL)):
        for name, pattern in rules:
            match = re.search(pattern, text, re.I)
            if match and not _is_negated(text, match.start()) and not (family in {"relationship", "affiliate"} and _is_editorial_disclosure(text, match.start(), match.end())):
                evidence.append(_evidence(family, name, caption[max(0, match.start() - 45): match.end() + 90]))
    hashtags += _HASHTAG_RE.findall(caption)
    mentions += [value for value in _MENTION_RE.findall(caption) if value.casefold() not in {item.casefold() for item in mentions}]
    for tag in hashtags:
        low = tag.casefold()
        if any(low.endswith(suffix) and len(low) > len(suffix) + 2 for suffix in ("sponsored", "partner", "partnership", "collab", "affiliate")):
            evidence.append(_evidence("hashtag", "compound hashtag", f"#{tag}"))
            explicit = explicit or low.endswith(("sponsored", "partnership", "partner"))
    paid_meta = post.get("paid_partnership")
    if paid_meta is True or paid_meta == 1:
        evidence.append(_evidence("metadata", "paid partnership metadata", "paid_partnership=true", "metadata"))
        explicit = True
    keyword_match = _extract_cta_keyword(caption)
    code_match = _CODE_RE.search(caption)
    cta = {"action": "comment_or_dm", "keyword": keyword_match[1]} if keyword_match else None
    code = code_match.group(1) if code_match else None
    candidates = _candidate_names(caption, hashtags, mentions, urls)
    product = None
    product_match = re.search(r"(?i:try|check out|meet|conoce|presenting|introducing)\s+([A-Z][\w.-]{2,}(?:\s+[A-Z][\w.-]{2,}){0,3})", caption)
    if product_match:
        product = re.split(r"\s+(?:comment|reply|dm|for the link|link in bio)\b", product_match.group(1), maxsplit=1, flags=re.I)[0].strip(" .,!?\n")
    if explicit and negated_explicit:
        classification = "needs_review"
    elif explicit:
        classification = "disclosed"
    elif negated_explicit and not any(item["family"] not in {"negation", "editorial"} for item in evidence):
        classification = "not_promo"
    elif any(item["family"] == "affiliate" for item in evidence) and (candidates or urls or code):
        classification = "likely"
    elif any(item["family"] == "relationship" for item in evidence) and candidates and any(item["family"] in {"commercial", "cta", "affiliate"} for item in evidence):
        classification = "likely"
    elif candidates and any(item["family"] == "commercial" and item["rule"] == "built with" for item in evidence) and (code or _has_offer(caption, candidates)):
        classification = "likely"
    elif any(item["family"] == "relationship" for item in evidence) and candidates:
        classification = "needs_review"
    else:
        classification = "not_promo"
    return {
        "detector_version": DETECTOR_VERSION,
        "classification": classification,
        "is_promo": classification != "not_promo",
        "client_candidates": candidates,
        "client": candidates[0]["name"] if candidates else None,
        "product": product,
        "links": [{"url": url, "source": "caption" if url in caption_urls else "first_comment"} for url in dict.fromkeys(urls)],
        "cta": cta,
        "promo_code": code,
        "evidence": evidence,
        "signals": sorted({item["rule"] for item in evidence}),
    }
