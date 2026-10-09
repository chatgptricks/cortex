"""Client-shareable media kits containing only public profile and performance highlights.

The renderer performs no HTTP requests and never triggers data ingestion.
"""
from __future__ import annotations

import io
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from types import MappingProxyType
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("MediaKit", str(_FONT_DIR / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("MediaKitBold", str(_FONT_DIR / "VeraBd.ttf")))
_GLYPHS = pdfmetrics.getFont("MediaKit").face.charToGlyph

DEFAULT_ACCENT = "#00A991"
W, H = A4
MARGIN = 39
CW = W - MARGIN * 2


def _rgb(value: str) -> tuple[float, float, float]:
    return tuple(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _contrast(first: str, second: str) -> float:
    def luminance(value: str) -> float:
        channels = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                    for channel in _rgb(value)]
        return sum(channel * weight for channel, weight in zip(channels, (0.2126, 0.7152, 0.0722)))
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _blend(first: str, second: str, weight: float) -> str:
    channels = [round((left * (1 - weight) + right * weight) * 255)
                for left, right in zip(_rgb(first), _rgb(second))]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


@dataclass(frozen=True)
class _Palette:
    # Immutable hex values prevent one report's theme from leaking into another.
    # Each access returns a fresh ReportLab Color; callers cannot mutate a
    # shared color object through this palette.
    values: Any

    def __getattr__(self, role: str) -> Any:
        if role not in self.values:
            raise AttributeError(role)
        return colors.HexColor(self.values[role])


def _make_palette(theme: str, accent: str) -> _Palette:
    dark = theme == "dark"
    accent = accent.upper() if isinstance(accent, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", accent) else DEFAULT_ACCENT
    palette = {
        "background": "#0D1723" if dark else "#F7F9FC",
        "card": "#172638" if dark else "#FFFFFF",
        "title": "#F0F5FA" if dark else "#11233B",
        "ink": "#DAE5EF" if dark else "#233751",
        "muted": "#A4B6C9" if dark else "#65758A",
        "line": "#384C61" if dark else "#DCE4EE",
        "alternate": "#1D3045" if dark else "#EFF4F9",
        "header": "#263D57" if dark else "#11233B",
        "header_ink": "#FFFFFF",
        "hero": "#182D45" if dark else "#11233B",
        "hero_ink": "#FFFFFF",
        "hero_muted": "#B8D7DD",
        "hero_body": "#E1EBF0",
        "accent": accent,
    }
    palette["accent_ink"] = max(("#000000", "#FFFFFF"), key=lambda value: _contrast(value, accent))
    palette["accent_soft"] = _blend(palette["card"], accent, 0.18 if dark else 0.10)
    accent_text = accent
    for step in range(101):
        candidate = _blend(accent, palette["title"], step / 100)
        if min(_contrast(candidate, palette[role]) for role in ("background", "card")) >= 4.5:
            accent_text = candidate
            break
    palette["accent_text"] = accent_text
    return _Palette(MappingProxyType(palette))


def _text(value: Any) -> str:
    if value is None:
        return "Not available"
    value = unicodedata.normalize("NFC", str(value))
    value = value.replace("\u2013", "-").replace("\u2014", "-").replace("\u2011", "-")
    value = re.sub(r"[\x00-\x08\x0b-\x1f]", "", value)
    # Skip unsupported emoji instead of emitting missing-glyph black boxes.
    return "".join(char for char in value if char in "\n\t" or ord(char) in _GLYPHS)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: Any, unit: str = "", compact: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "N/A"
    suffix = "%" if "percent" in unit.lower() or unit.lower() in ("%", "pct") else ""
    if 0 < abs(number) < 0.005:
        return ("<0.01" if number > 0 else ">-0.01") + suffix
    if compact and not suffix and abs(number) >= 1_000:
        for scale, label in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if abs(number) >= scale:
                return f"{number / scale:,.1f}".removesuffix(".0") + label
    if compact and not suffix:
        if number == int(number):
            return f"{number:,.0f}"
        return f"{number:,.2f}" if abs(number) < 1 else f"{number:,.1f}"
    if suffix:
        return f"{number:,.2f}%"
    if number == int(number):
        return f"{number:,.0f}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _date(value: Any, with_time: bool = False, timezone_name: str = "America/Costa_Rica") -> str:
    if not value:
        return "Not available"
    try:
        raw = str(value)
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if len(raw) > 10:
            stamp = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo(timezone_name))
        return stamp.strftime("%d %b %Y, %H:%M" if with_time else "%d %b %Y")
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return _text(value)[:32]


def _stat(period: dict[str, Any], metric: str, statistic: str = "total") -> Any:
    return (period.get("metrics", {}).get(metric) or {}).get(statistic)


def _url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    return value if parsed.scheme in ("https", "http") and parsed.hostname else None


def _instagram_profile(handle: Any) -> str | None:
    return f"https://www.instagram.com/{handle}/" if isinstance(handle, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,30}", handle) else None


def _instagram_post(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in ("instagram.com", "www.instagram.com"):
        return None
    if not re.fullmatch(r"/(p|reel)/[A-Za-z0-9_-]+/?", parsed.path):
        return None
    return "https://www.instagram.com" + parsed.path.rstrip("/") + "/"


class _Report:
    def __init__(self, report: dict[str, Any], *, theme: str = "light", accent: str = DEFAULT_ACCENT):
        self.p = _make_palette(theme, accent)
        self.report = report
        self.account = report.get("account") or {}
        self.summary = report.get("summary") or {}
        self.all_time = self.summary.get("all_time") or {}
        self.recent = self.summary.get("last_30_days") or {}
        # The public projection removes the recent key when its publication
        # window is not complete. Missing is different from an asserted zero.
        self.recent_available = "last_30_days" in self.summary
        self.handle = _text(self.account.get("handle") or "Account").lstrip("@")
        self.stream = io.BytesIO()
        self.c = canvas.Canvas(self.stream, pagesize=A4, pageCompression=1, invariant=True)
        try:
            generated = datetime.fromisoformat(str(report.get("generated_at")).replace("Z", "+00:00"))
            generated = (generated if generated.tzinfo else generated.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
            stamp = self.c._doc._timeStamp
            stamp.t = generated.timestamp()
            stamp.lt = generated.utctimetuple()
            stamp.YMDhms = tuple(stamp.lt)[:6]
            stamp.dhh = stamp.dmm = 0
            stamp.tzname = "UTC"
        except (TypeError, ValueError, OverflowError):
            pass
        self.c.setTitle(f"@{self.handle} | Sentient Media Kit")
        self.c.setAuthor("Sentient")
        self.c.setSubject("Public account media kit for brand partnerships")
        self.pages: list[dict[str, Any]] = []
        self.page_number = 0
        self.y = H - 100
        self.generated = _date(report.get("generated_at"), with_time=True, timezone_name=report.get("timezone") or "America/Costa_Rica")

    def text(self, x: float, y: float, value: Any, size: float = 9,
             color: Any = None, bold: bool = False, width: float | None = None,
             align: str = "left") -> None:
        value = _text(value).replace("\n", " ")
        face = "MediaKitBold" if bold else "MediaKit"
        if width and pdfmetrics.stringWidth(value, face, size) > width:
            while value and pdfmetrics.stringWidth(value + "...", face, size) > width:
                value = value[:-1]
            value += "..."
        self.c.setFont(face, size)
        self.c.setFillColor(self.p.ink if color is None else color)
        if align == "right":
            self.c.drawRightString(x, y, value)
        elif align == "center":
            self.c.drawCentredString(x, y, value)
        else:
            self.c.drawString(x, y, value)

    def lines(self, value: Any, width: float, size: float = 9, bold: bool = False) -> list[str]:
        face = "MediaKitBold" if bold else "MediaKit"
        output: list[str] = []
        for paragraph in _text(value).splitlines() or [""]:
            current = ""
            for word in paragraph.split():
                candidate = f"{current} {word}".strip()
                if pdfmetrics.stringWidth(candidate, face, size) <= width:
                    current = candidate
                else:
                    if current:
                        output.append(current)
                    current = word
                    while pdfmetrics.stringWidth(current, face, size) > width:
                        split = len(current)
                        while split > 1 and pdfmetrics.stringWidth(current[:split], face, size) > width:
                            split -= 1
                        output.append(current[:split])
                        current = current[split:]
            if current:
                output.append(current)
        return output or [""]

    def paragraph(self, x: float, y: float, value: Any, width: float,
                  size: float = 9, color: Any = None, max_lines: int | None = None,
                  leading: float | None = None, bold: bool = False) -> float:
        rows = self.lines(value, width, size, bold)
        color = self.p.muted if color is None else color
        if max_lines and len(rows) > max_lines:
            rows = rows[:max_lines]
            rows[-1] = rows[-1].rstrip(".") + "..."
        step = leading or size * 1.5
        for row in rows:
            self.text(x, y, row, size, color, bold, width)
            y -= step
        return y

    def rect(self, x: float, y: float, width: float, height: float,
             fill: Any = None, radius: float = 9) -> None:
        fill = self.p.card if fill is None else fill
        self.c.setFillColor(fill)
        outline = fill == self.p.accent and _contrast(self.p.values["accent"], self.p.values["card"]) < 3
        if outline:
            self.c.setStrokeColor(self.p.accent_text)
            self.c.setLineWidth(0.6)
        self.c.roundRect(x, y, width, height, radius, fill=1, stroke=int(outline))

    def link(self, x: float, y: float, label: str, url: Any, size: float = 8) -> None:
        safe = _url(url)
        if not safe:
            return
        self.text(x, y, label, size, self.p.accent_text, bold=True)
        self.c.linkURL(safe, (x, y - 3, x + pdfmetrics.stringWidth(label, "MediaKitBold", size), y + size + 2), relative=0)

    def image(self, raw: Any, x: float, y: float, width: float, height: float) -> bool:
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            return False
        try:
            source = ImageReader(io.BytesIO(raw))
            source.getSize()
            self.c.drawImage(source, x, y, width, height, preserveAspectRatio=True, anchor="c", mask="auto")
            return True
        except Exception:
            # Media is optional; a missing or stale owned thumbnail must not
            # prevent users from downloading the measured report.
            return False

    def page(self, title: str, subtitle: str) -> None:
        if self.page_number:
            self.pages.append(dict(self.c.__dict__))
            self.c._startPage()
        self.page_number += 1
        self.c.setFillColor(self.p.background)
        self.c.rect(0, 0, W, H, fill=1, stroke=0)
        self.rect(MARGIN, H - 49, 18, 18, self.p.accent, radius=5)
        self.text(MARGIN + 5, H - 43, "S", 10, self.p.accent_ink, bold=True)
        self.text(MARGIN + 27, H - 42, "SENTIENT", 10, self.p.title, bold=True)
        self.text(W - MARGIN, H - 42, "MEDIA KIT", 8, self.p.muted, align="right")
        self.text(MARGIN, H - 91, title, 23, self.p.title, bold=True, width=CW)
        self.paragraph(MARGIN, H - 109, subtitle, CW, 8.2, max_lines=2)
        self.y = H - 145

    def section(self, title: str, y: float) -> float:
        self.text(MARGIN, y, title, 13, self.p.title, bold=True)
        return y - 20

    def icon(self, kind: str, x: float, y: float, size: float = 20, color: Any = None) -> None:
        """Draw small, sharp vector icons without font-dependent emoji."""
        c = self.c
        c.saveState()
        c.translate(x, y)
        c.scale(size / 24, size / 24)
        c.setStrokeColor(self.p.accent_text if color is None else color)
        c.setFillColor(self.p.accent_text if color is None else color)
        c.setLineWidth(1.6)
        c.setLineCap(1)
        c.setLineJoin(1)
        if kind == "people":
            c.circle(8, 17, 3.4, fill=0, stroke=1)
            c.circle(17, 16, 2.7, fill=0, stroke=1)
            path = c.beginPath(); path.moveTo(2, 4); path.lineTo(2, 7)
            path.curveTo(2, 13, 14, 13, 14, 7); path.lineTo(14, 4)
            path.moveTo(17, 12); path.curveTo(21, 12, 23, 9, 22, 4)
            c.drawPath(path)
        elif kind == "heart":
            path = c.beginPath(); path.moveTo(12, 3)
            path.curveTo(9, 6, 2, 11, 2, 16); path.curveTo(2, 23, 10, 23, 12, 18)
            path.curveTo(14, 23, 22, 23, 22, 16); path.curveTo(22, 11, 15, 6, 12, 3)
            c.drawPath(path)
        elif kind == "comment":
            c.roundRect(2, 7, 20, 15, 4, fill=0, stroke=1)
            path = c.beginPath(); path.moveTo(7, 7); path.lineTo(5, 2); path.lineTo(13, 7)
            c.drawPath(path)
            c.line(7, 16, 17, 16); c.line(7, 12, 14, 12)
        elif kind == "eye":
            path = c.beginPath(); path.moveTo(1, 12)
            path.curveTo(7, 22, 17, 22, 23, 12); path.curveTo(17, 2, 7, 2, 1, 12)
            c.drawPath(path); c.circle(12, 12, 3.5, fill=0, stroke=1)
        elif kind == "play":
            c.circle(12, 12, 10, fill=0, stroke=1)
            path = c.beginPath(); path.moveTo(9, 7); path.lineTo(18, 12); path.lineTo(9, 17); path.close()
            c.drawPath(path, fill=1, stroke=0)
        else:
            c.roundRect(3, 3, 18, 18, 3, fill=0, stroke=1)
            c.line(3, 15, 21, 15); c.line(12, 3, 12, 15)
        c.restoreState()

    @staticmethod
    def metric_icon(label: str) -> str:
        label = label.lower()
        for word, kind in (("follower", "people"), ("like", "heart"), ("comment", "comment"),
                           ("view", "eye"), ("play", "play")):
            if word in label:
                return kind
        return "posts"

    def cards(self, items: list[tuple[str, Any, str, str]], y: float, columns: int = 3,
              height: float = 79, style: str = "average") -> float:
        gap = 10
        width = (CW - (columns - 1) * gap) / columns
        for index, (label, value, unit, detail) in enumerate(items):
            col, row = index % columns, index // columns
            x, top = MARGIN + col * (width + gap), y - row * (height + gap)
            self.rect(x, top - height, width, height)
            if style == "total":
                self.icon(self.metric_icon(label), x + 13, top - 44, 26)
                self.text(x + 50, top - 20, label.upper(), 7.2, self.p.muted, bold=True, width=width - 62)
                self.text(x + 50, top - 46, _fmt(value, unit, compact=True), 20, self.p.title, bold=True, width=width - 62)
                self.text(x + 50, top - 61, detail, 6.4, self.p.muted, width=width - 62)
            else:
                self.icon(self.metric_icon(label), x + 12, top - 28, 18 if style == "recent" else 21)
                self.text(x + 38, top - 23, label.upper(), 6.7 if style == "recent" else 7.1,
                          self.p.muted, bold=True, width=width - 50)
                self.text(x + 12, top - 51, _fmt(value, unit, compact=True), 19 if style == "recent" else 23,
                          self.p.title, bold=True, width=width - 24)
                self.text(x + 12, top - 69, detail, 6.1 if style == "recent" else 6.4,
                          self.p.muted, width=width - 24)
        return y - math.ceil(len(items) / columns) * (height + gap)

    def note(self, text: Any, y: float, height: float = 47) -> float:
        self.rect(MARGIN, y - height, CW, height, self.p.accent_soft)
        self.paragraph(MARGIN + 12, y - 16, text, CW - 24, 7.5, self.p.ink, max_lines=4)
        return y - height - 14

    def overview(self) -> None:
        self.page("Audience & performance", "Public audience and content highlights for brand partnerships.")
        top = self.y + 2
        self.rect(MARGIN, top - 156, CW, 156, self.p.hero, radius=12)
        avatar = self.image(self.account.get("avatar_bytes"), MARGIN + 20, top - 85, 59, 59)
        hero_x = MARGIN + (95 if avatar else 20)
        hero_width = CW - (263 if avatar else 188)
        name = self.account.get("public_name") or f"@{self.handle}"
        self.text(hero_x, top - 38, name, 21.5, self.p.hero_ink, bold=True, width=hero_width)
        profile_line = f"@{self.handle} / Instagram" if self.account.get("public_name") != f"@{self.handle}" else "Instagram"
        self.text(hero_x, top - 62, profile_line, 9.5, self.p.hero_muted, width=hero_width)
        if _number(self.account.get("followers")) is not None:
            audience_x = MARGIN + CW - 147
            self.icon("people", audience_x, top - 40, 21, self.p.hero_muted)
            self.text(audience_x + 30, top - 32, "FOLLOWERS", 7.5, self.p.hero_muted, bold=True)
            self.text(audience_x, top - 82, _fmt(self.account["followers"], compact=True), 34,
                      self.p.hero_ink, bold=True, width=129)
        bio = self.account.get("public_bio")
        if bio:
            self.paragraph(MARGIN + 20, top - 108, bio, CW - 40, 8.2,
                           self.p.hero_body, max_lines=2, leading=12)
        else:
            self.text(MARGIN + 20, top - 110, "Explore the public profile and selected content highlights.",
                      8.2, self.p.hero_body, width=CW - 40)
        profile_url = _instagram_profile(self.handle)
        if profile_url:
            self.c.linkURL(profile_url, (MARGIN, top - 156, W - MARGIN, top), relative=0)
        growth = ((self.report.get("follower_growth") or {}).get("30d") or {}).get("pct")
        if _number(growth) is not None:
            value = _fmt(growth, "percent")
            if _number(growth) > 0:
                value = "+" + value
            self.text(MARGIN + 20, top - 141, f"{value} audience change / last 30 days",
                      8, self.p.hero_muted, width=CW - 40)
        video_key = "video_views" if _number(_stat(self.all_time, "video_views", "average")) is not None else "video_plays"
        video_label = "video views" if video_key == "video_views" else "video plays"
        average_candidates = [
            ("Average likes", _stat(self.all_time, "likes", "average"), "count", "Per public post"),
            ("Average comments", _stat(self.all_time, "comments", "average"), "count", "Per public post"),
            (f"Average {video_label}", _stat(self.all_time, video_key, "average"), "count", "Per video with public counts"),
        ]
        total_candidates = [
            ("Total likes", _stat(self.all_time, "likes"), "count", "Analyzed public posts"),
            (f"Total {video_label}", _stat(self.all_time, video_key), "count", "Analyzed public videos"),
        ]
        averages = [item for item in average_candidates if _number(item[1]) is not None]
        totals = [item for item in total_candidates if _number(item[1]) is not None]
        y = self.section("Typical content performance", top - 180)
        if averages:
            y = self.cards(averages, y, columns=len(averages), height=83 if self.recent_available else 92)
        if totals:
            y = self.cards(totals, y + 1, columns=len(totals), height=72 if self.recent_available else 84, style="total")
        if not averages and not totals:
            y = self.paragraph(MARGIN, y - 15, "Public performance highlights are not available yet.",
                               CW, 10, self.p.muted) - 40
        if self.recent_available:
            y = self.section("Posts published in the last 30 days", y - 8)
            recent_video = "video_views" if _number(_stat(self.recent, "video_views")) is not None else "video_plays"
            recent_candidates = [
                ("Public posts", self.recent.get("post_count"), "count", "Published in this period"),
                ("Likes", _stat(self.recent, "likes"), "count", "Across those posts"),
                ("Comments", _stat(self.recent, "comments"), "count", "Across those posts"),
                ("Video views" if recent_video == "video_views" else "Video plays", _stat(self.recent, recent_video), "count", "Across those videos"),
            ]
            recent_items = [item for item in recent_candidates if _number(item[1]) is not None]
            if recent_items:
                y = self.cards(recent_items, y, columns=len(recent_items), height=79, style="recent")
            note = "Historical highlights summarize analyzed public posts. Recent figures cover posts published in the last 30 days and their current cumulative public counts. Video views and plays are separate measures; neither represents unique reach."
        else:
            note = "Historical highlights summarize analyzed public posts and their current cumulative public counts. Video views and plays are separate measures; neither represents unique reach."
        self.note(note, min(y - 18, 173 if self.recent_available else 253), height=63)

    def post_card(self, post: dict[str, Any] | None, x: float, y: float, width: float, rank: int,
                  empty_message: str = "Public post highlights are not available for this period.") -> None:
        wide = width > CW * 0.75
        self.rect(x, y - (168 if wide else 155), width, 168 if wide else 155)
        if not post:
            self.paragraph(x + 15, y - 42, empty_message,
                           width - 30, 9, self.p.muted, max_lines=3)
            return
        self.text(x + 12, y - 18, f"{rank:02}", 8.5, self.p.accent_text, bold=True)
        format_name = post.get("format") if post.get("format") in ("Carousel", "Image", "Reel", "Video", "Post") else "Post"
        self.text(x + width - 12, y - 18, f"{_date(post.get('published_at'))} / {format_name}",
                  6.9, self.p.muted, width=width - 50, align="right")
        caption = post.get("public_caption") or "View this public post"
        image_size = 108 if wide else 82
        has_image = self.image(post.get("thumbnail_bytes"), x + 12, y - (135 if wide else 107), image_size, image_size)
        content_x = x + (140 if has_image and wide else 107 if has_image else 12)
        self.paragraph(content_x, y - 43, caption,
                       width - (content_x - x) - 12, 10.3 if wide else 8.3, self.p.title,
                       max_lines=4 if wide else 5 if has_image else 4, leading=14 if wide else 11.5, bold=True)
        metrics = post.get("metrics") or {}
        def badges(keys: tuple[str, str], at: float) -> None:
            offset = x + (140 if wide and has_image else 12)
            for key in keys:
                value = _number(metrics.get(key))
                if value is None:
                    continue
                label = {"likes": "likes", "comments": "comments", "video_views": "views", "video_plays": "plays"}[key]
                rendered = f"{_fmt(value, compact=True)} {label}"
                self.icon(self.metric_icon(label), offset, at - 3, 13 if wide else 11)
                self.text(offset + 17 if wide else offset + 15, at, rendered, 8.3 if wide else 7, self.p.muted)
                offset += 25 + pdfmetrics.stringWidth(rendered, "MediaKit", 8.3 if wide else 7)
        badges(("likes", "comments"), y - (113 if wide else 122))
        badges(("video_views", "video_plays"), y - (130 if wide else 135))
        url = _instagram_post(post.get("permalink"))
        if url:
            self.link(x + (140 if wide and has_image else 12), y - (153 if wide else 146), "VIEW PUBLIC POST", url, size=7.5 if wide else 6.9)

    def strongest_posts(self) -> None:
        self.page("Content that connects", "Selected public posts that showcase the account's content and engagement.")
        groups = self.report.get("best_posts") or {}
        historical = (groups.get("all_time") or [])[:3]
        if not self.recent_available:
            self.text(MARGIN, self.y, "HISTORICAL HIGHLIGHTS", 8, self.p.accent_text, bold=True)
            y = self.y - 15
            for index in range(max(len(historical), 1)):
                self.post_card(historical[index] if index < len(historical) else None, MARGIN, y, CW, index + 1)
                y -= 181
            self.note("Highlights are selected by public likes and comments. Displayed public counts include cumulative activity since publication.",
                      min(y - 5, 170), height=49)
            return
        recent = (groups.get("last_30_days") or [])[:3]
        width = (CW - 13) / 2
        self.text(MARGIN, self.y, "HISTORICAL HIGHLIGHTS", 8, self.p.accent_text, bold=True)
        self.text(MARGIN + width + 13, self.y, "PUBLISHED IN THE LAST 30 DAYS", 7.6, self.p.accent_text, bold=True)
        y = self.y - 15
        rows = max(len(historical), len(recent), 1)
        for index in range(rows):
            for posts, x in ((historical, MARGIN), (recent, MARGIN + width + 13)):
                if index < len(posts):
                    self.post_card(posts[index], x, y, width, index + 1)
                elif index == 0:
                    message = "No public posts were published in this period." if posts is recent and self.recent.get("post_count") == 0 else "Public post highlights are not available for this period."
                    self.post_card(None, x, y, width, index + 1, empty_message=message)
            y -= 168
        self.note("Highlights are selected by public likes and comments. Recent posts were published in the last 30 days; displayed counts include activity since publication.",
                  min(y - 5, 170), height=49)

    def finish(self) -> bytes:
        self.pages.append(dict(self.c.__dict__))
        total = len(self.pages)
        for index, state in enumerate(self.pages, start=1):
            self.c.__dict__.update(state)
            self.c.setStrokeColor(self.p.line)
            self.c.setLineWidth(0.6)
            self.c.line(MARGIN, 51, W - MARGIN, 51)
            prepared = f" / Prepared {_date(self.report['generated_at'])}" if self.report.get("generated_at") else ""
            self.text(MARGIN, 33, f"@{self.handle}{prepared}", 6.5, self.p.muted, width=CW - 90)
            self.text(W - MARGIN, 33, f"{index:02} / {total:02}", 6.5, self.p.muted, align="right")
            self.c.showPage()
        self.c.save()
        return self.stream.getvalue()


def render_media_kit_pdf(report: dict[str, Any], *, theme: str = "light", accent: str = DEFAULT_ACCENT) -> bytes:
    """Return a two-page, client-shareable account media kit as bytes.

    Rendering uses a fixed public-field allowlist and never traverses metric
    inventories, account configuration, contact data or internal post labels.
    """
    document = _Report(report, theme=theme, accent=accent)
    document.overview()
    document.strongest_posts()
    return document.finish()
