from __future__ import annotations

import html
import json
import os
import pickle
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse


class InstagramImportError(RuntimeError):
    pass


@dataclass
class InstagramPostImport:
    url: str
    shortcode: str
    caption: str | None
    title: str
    image_url: str
    image_bytes: bytes
    image_suffix: str
    image_content_type: str | None


SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)
JSON_LD_RE = re.compile(
    r"<script\s+[^>]*type=[\"']application/ld\+json[\"'][^>]*>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
EMBEDDED_IMAGE_PATTERNS = (
    re.compile(r'"display_url"\s*:\s*"(?P<url>(?:\\.|[^"\\])*)"', re.IGNORECASE),
    re.compile(r'"thumbnail_src"\s*:\s*"(?P<url>(?:\\.|[^"\\])*)"', re.IGNORECASE),
    re.compile(r'"thumbnail_url"\s*:\s*"(?P<url>(?:\\.|[^"\\])*)"', re.IGNORECASE),
)
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
IG_API_HEADERS = {
    "User-Agent": (
        "Instagram 219.0.0.12.117 Android (29/10; 420dpi; 1080x1920; "
        "samsung; SM-G973F; beyond1; exynos9820; en_US; 346138351)"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US",
    "X-IG-App-ID": "936619743392459",
}
_COOKIE_CACHE: dict[str, str] | None = None


def fetch_instagram_post(
    url: str,
    timeout: float = 25.0,
    cover_image_url: str | None = None,
) -> InstagramPostImport:
    clean_url = _canonical_instagram_url(url)
    shortcode = _shortcode(clean_url)

    try:
        import httpx
    except ImportError as exc:
        raise InstagramImportError("httpx is not installed in the backend environment.") from exc

    caption: str | None = None
    image_url: str | None = _clean_url(cover_image_url)

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=WEB_HEADERS,
    ) as client:
        try:
            oembed = client.get(f"https://www.instagram.com/api/v1/oembed/?url={quote_plus(clean_url)}")
            if oembed.status_code == 200:
                payload = oembed.json()
                caption = _clean_caption(payload.get("title"))
                image_url = _clean_url(payload.get("thumbnail_url")) or image_url
        except Exception:
            pass

        if not caption or not image_url:
            response = client.get(clean_url)
            if response.status_code >= 400:
                raise InstagramImportError(f"Instagram returned HTTP {response.status_code} for that post URL.")
            html_text = response.text
            meta = _extract_meta(html_text)
            caption = caption or _clean_caption(meta.get("og:description") or meta.get("description"))
            image_url = image_url or _clean_url(meta.get("og:image") or meta.get("twitter:image"))
            json_caption, json_image = _extract_json_ld(html_text)
            caption = caption or json_caption
            image_url = image_url or json_image
            image_url = image_url or _extract_embedded_image(html_text)

        if not caption or not image_url:
            embed = client.get(_instagram_embed_url(clean_url))
            if embed.status_code < 400:
                embed_text = embed.text
                meta = _extract_meta(embed_text)
                caption = caption or _clean_caption(meta.get("og:description") or meta.get("description"))
                image_url = image_url or _clean_url(meta.get("og:image") or meta.get("twitter:image"))
                json_caption, json_image = _extract_json_ld(embed_text)
                caption = caption or json_caption
                image_url = image_url or json_image
                image_url = image_url or _extract_embedded_image(embed_text)

        if not caption or not image_url:
            api_caption, api_image_url = _fetch_from_instagram_api(shortcode, timeout)
            caption = caption or api_caption
            image_url = image_url or api_image_url

        if not image_url:
            raise InstagramImportError("Could not find a cover image on the Instagram post.")

        image = client.get(image_url)
        if image.status_code >= 400:
            raise InstagramImportError(f"Instagram image download returned HTTP {image.status_code}.")
        content_type = image.headers.get("content-type")
        suffix = _image_suffix(image_url, content_type)
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise InstagramImportError(f"Instagram cover image type is not supported: {content_type or suffix}.")

    title = _title_from_caption(caption) or f"Instagram post {shortcode}"
    return InstagramPostImport(
        url=clean_url,
        shortcode=shortcode,
        caption=caption,
        title=title,
        image_url=image_url,
        image_bytes=image.content,
        image_suffix=suffix,
        image_content_type=content_type,
    )


def _canonical_instagram_url(value: str) -> str:
    url = value.strip()
    if not url:
        raise InstagramImportError("Instagram URL is required.")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") != "instagram.com":
        raise InstagramImportError("Paste a valid instagram.com post, reel, or tv URL.")
    shortcode = _shortcode(url)
    kind = parsed.path.strip("/").split("/")[0]
    return f"https://www.instagram.com/{kind}/{shortcode}/"


def _shortcode(url: str) -> str:
    match = SHORTCODE_RE.search(url)
    if not match:
        raise InstagramImportError("Could not read the Instagram shortcode from that URL.")
    return match.group(1)


def _instagram_embed_url(url: str) -> str:
    return f"{url.rstrip('/')}/embed/captioned/"


def _fetch_from_instagram_api(shortcode: str, timeout: float) -> tuple[str | None, str | None]:
    cookies = _load_instagram_cookies()
    if not cookies:
        return None, None

    try:
        import httpx
    except ImportError as exc:
        raise InstagramImportError("httpx is not installed in the backend environment.") from exc

    media_id = _shortcode_to_media_id(shortcode)
    url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
    headers = {**IG_API_HEADERS, "Referer": f"https://www.instagram.com/p/{shortcode}/"}
    try:
        response = httpx.get(url, headers=headers, cookies=cookies, timeout=timeout, follow_redirects=True)
    except Exception:
        return None, None
    if response.status_code >= 400:
        return None, None
    try:
        payload = response.json()
    except Exception:
        return None, None

    items = payload.get("items") if isinstance(payload, dict) else None
    item = items[0] if isinstance(items, list) and items else None
    if not isinstance(item, dict):
        return None, None

    caption = _clean_caption((item.get("caption") or {}).get("text") if isinstance(item.get("caption"), dict) else None)
    first_media = item
    carousel_media = item.get("carousel_media")
    if isinstance(carousel_media, list) and carousel_media:
        first_candidate = carousel_media[0]
        if isinstance(first_candidate, dict):
            first_media = first_candidate
    return caption, _best_image_candidate(first_media)


def _shortcode_to_media_id(shortcode: str) -> str:
    media_id = 0
    for character in shortcode:
        if character not in SHORTCODE_ALPHABET:
            raise InstagramImportError("Instagram shortcode contains an unsupported character.")
        media_id = (media_id * 64) + SHORTCODE_ALPHABET.index(character)
    return str(media_id)


def _best_image_candidate(media: dict[str, Any]) -> str | None:
    candidates = (media.get("image_versions2") or {}).get("candidates")
    if not isinstance(candidates, list):
        return None
    valid = [candidate for candidate in candidates if isinstance(candidate, dict) and _clean_url(candidate.get("url"))]
    if not valid:
        return None
    best = max(valid, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
    return _clean_url(best.get("url"))


def _load_instagram_cookies() -> dict[str, str]:
    global _COOKIE_CACHE
    if _COOKIE_CACHE is not None:
        return _COOKIE_CACHE

    cookies = _load_instaloader_session_cookies()
    if not cookies:
        cookies = _load_browser_cookies()
    _COOKIE_CACHE = cookies
    return cookies


def _load_instaloader_session_cookies() -> dict[str, str]:
    for session_file in _candidate_session_files():
        try:
            raw = pickle.loads(session_file.read_bytes())
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        cookies = {
            str(name): str(value)
            for name, value in raw.items()
            if isinstance(name, str) and isinstance(value, str) and value
        }
        if cookies.get("sessionid"):
            return cookies
    return {}


def _candidate_session_files() -> list[Path]:
    env_paths = [
        os.getenv("INSTAGRAM_SESSION_FILE", ""),
        os.getenv("INSTALOADER_SESSION_FILE", ""),
    ]
    files: list[Path] = []
    for raw_path in env_paths:
        if raw_path.strip():
            path = Path(raw_path).expanduser()
            files.append(path if path.is_absolute() else Path.cwd() / path)

    app_root = Path(__file__).resolve().parents[2]
    search_roots = [
        app_root / ".instaloader",
        Path.home() / ".instaloader",
        Path.home() / "Desktop" / "Codex Projects",
    ]
    for root in search_roots:
        if not root.exists():
            continue
        if root.name == ".instaloader":
            files.extend(root.glob("session-*"))
        else:
            for depth in ("*/.instaloader/session-*", "*/*/.instaloader/session-*", "*/*/*/.instaloader/session-*"):
                files.extend(root.glob(depth))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.expanduser()
        if resolved in seen or not resolved.exists() or not resolved.is_file():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _load_browser_cookies() -> dict[str, str]:
    browser = (
        os.getenv("INSTAGRAM_COOKIE_BROWSER", "")
        or os.getenv("INSTALOADER_BROWSER", "")
        or ""
    ).strip().lower().replace("-", "_")
    if not browser:
        return {}
    try:
        import browser_cookie3
    except ImportError:
        return {}
    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        return {}
    try:
        jar = loader(domain_name=".instagram.com")
    except Exception:
        return {}
    return {
        cookie.name: cookie.value
        for cookie in jar
        if "instagram" in cookie.domain and cookie.value
    }


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        attributes = {name.lower(): value for name, value in attrs if value is not None}
        name = (attributes.get("property") or attributes.get("name") or "").lower()
        content = attributes.get("content")
        if name in {"og:image", "og:description", "twitter:image", "description"} and content:
            self.values[name] = content


def _extract_meta(html_text: str) -> dict[str, str]:
    parser = _MetaParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    return parser.values


def _extract_json_ld(html_text: str) -> tuple[str | None, str | None]:
    for match in JSON_LD_RE.finditer(html_text):
        try:
            payload = json.loads(html.unescape(match.group("body")).strip())
        except Exception:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            caption = _clean_caption(item.get("caption") or item.get("description") or item.get("name"))
            image = item.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            elif isinstance(image, dict):
                image = image.get("url")
            image_url = _clean_url(image)
            if caption or image_url:
                return caption, image_url
    return None, None


def _extract_embedded_image(html_text: str) -> str | None:
    for pattern in EMBEDDED_IMAGE_PATTERNS:
        for match in pattern.finditer(html_text):
            try:
                value = json.loads(f'"{match.group("url")}"')
            except (json.JSONDecodeError, TypeError):
                continue
            image_url = _clean_url(value)
            if image_url:
                return image_url
    return None


def _clean_caption(value: Any) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text


def _clean_url(value: Any) -> str | None:
    if value is None:
        return None
    url = html.unescape(str(value)).strip()
    return url if url.startswith("http") else None


def _image_suffix(url: str, content_type: str | None) -> str:
    if content_type:
        kind = content_type.split(";", 1)[0].strip().lower()
        if kind == "image/jpeg":
            return ".jpg"
        if kind == "image/png":
            return ".png"
        if kind == "image/webp":
            return ".webp"
    suffix = Path(urlparse(url).path).suffix.lower()
    return ".jpg" if suffix in {".jpg", ".jpeg", ""} else suffix


def _title_from_caption(caption: str | None) -> str | None:
    if not caption:
        return None
    for line in caption.splitlines():
        clean = line.strip()
        if clean:
            return clean[:120]
    return caption.strip()[:120] or None
