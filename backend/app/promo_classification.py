"""Public Promo classification matching Research's manual flag and hashtag."""
from __future__ import annotations

import re
from typing import Any

# JavaScript /#aitoolsentient\b/i uses ASCII word boundaries and case folding.
_PROMO_HASHTAG = re.compile(r"#aitoolsentient\b", re.IGNORECASE | re.ASCII)


def is_public_promo(post: dict[str, Any]) -> bool:
    """Use the authoritative manual flag and only the published caption.

    Normalized reports preserve the source of manual curation separately from
    refreshed metric copies. Legacy projection inputs can use is_promo directly.
    Never infer a public tag from an internal title, hook or generated caption.
    """
    manual = post.get("_public_manual_promo", post.get("is_promo"))
    marked = manual is True or manual == 1 or isinstance(manual, str) and manual.strip().lower() in {"true", "1", "yes"}
    caption = post.get("public_caption")
    return bool(marked or isinstance(caption, str) and _PROMO_HASHTAG.search(caption))
