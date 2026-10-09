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
pdfmetrics.registerFont(TTFont("MediaKitDisplay", str(Path(__file__).parent / "assets" / "fonts" / "Anton-Regular.ttf")))
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
        "background": "#080F17" if dark else "#F8FAFC",
        "card": "#102033" if dark else "#FFFFFF",
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

    def avatar(self, raw: Any, x: float, y: float, size: float) -> bool:
        self.c.saveState()
        clipping = self.c.beginPath()
        clipping.circle(x + size / 2, y + size / 2, size / 2)
        self.c.clipPath(clipping, stroke=0, fill=0)
        drawn = self.image(raw, x, y, size, size)
        self.c.restoreState()
        return drawn

    def page(self, title: str, subtitle: str) -> None:
        if self.page_number:
            self.pages.append(dict(self.c.__dict__))
            self.c._startPage()
        self.page_number += 1
        self.c.setFillColor(self.p.background)
        self.c.rect(0, 0, W, H, fill=1, stroke=0)
        self.rect(MARGIN, H - 56, 27, 27, self.p.accent_soft, radius=14)
        if not self.avatar(self.account.get("avatar_bytes"), MARGIN + 2, H - 54, 23):
            self.icon("people", MARGIN + 5, H - 50, 17)
        self.text(MARGIN + 36, H - 42, f"@{self.handle}", 10, self.p.title, bold=True)
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

    def display(self, x: float, y: float, value: Any, size: float = 35,
                color: Any = None, width: float | None = None) -> None:
        """A bundled condensed display face gives headings editorial hierarchy."""
        value = _text(value)
        if width:
            size = min(size, size * width / max(pdfmetrics.stringWidth(value, "MediaKitDisplay", size), 1))
        self.c.setFillColor(self.p.title if color is None else color)
        self.c.setFont("MediaKitDisplay", size)
        self.c.drawString(x, y, value)

    def rule(self, x: float, y: float, width: float, color: Any = None) -> None:
        self.c.setStrokeColor(self.p.line if color is None else color)
        self.c.setLineWidth(0.6)
        self.c.line(x, y, x + width, y)

    def editorial_page(self, section: str) -> None:
        self.page("", "")
        self.text(MARGIN + 36, H - 57, "INSTAGRAM", 6.5, self.p.muted)
        self.text(W - MARGIN, H - 57, section.upper(), 7, self.p.accent_text, bold=True, align="right")
        self.rule(MARGIN, H - 73, CW)

    def strip(self, items: list[tuple[str, Any, str, str]], top: float,
              height: float = 86, number_size: float = 29) -> float:
        """Open metrics separated by thin rules, rather than identical tiles."""
        if not items:
            return top
        width = CW / len(items)
        self.rule(MARGIN, top, CW)
        self.rule(MARGIN, top - height, CW)
        for index, (label, value, unit, detail) in enumerate(items):
            x = MARGIN + index * width
            if index:
                self.c.setStrokeColor(self.p.line)
                self.c.line(x, top, x, top - height)
            self.icon(self.metric_icon(label), x + 11, top - 27, 13)
            self.text(x + 31, top - 22, label.upper(), 6.5, self.p.muted, bold=True, width=width - 41)
            number_y = top - (59 if height > 75 else 53 if detail else 49)
            self.display(x + 11, number_y,
                         _fmt(value, unit, compact=True), number_size, self.p.title, width - 22)
            if detail:
                self.text(x + 11, top - height + 11, detail, 6, self.p.muted, width=width - 22)
        return top - height

    def profile_card(self, x: float, top: float, width: float, height: float) -> None:
        self.rect(x, top - height, width, height, radius=12)
        self.rect(x + 17, top - 86, 65, 65, self.p.accent_soft, radius=33)
        if not self.avatar(self.account.get("avatar_bytes"), x + 20, top - 83, 59):
            self.icon("people", x + 37, top - 66, 26)
        self.text(x + 94, top - 44, "INSTAGRAM", 6.5, self.p.accent_text, bold=True)
        if self.account.get("verified") is True:
            self.text(x + 94, top - 60, "Verified profile", 7.2, self.p.muted)
        self.text(x + 17, top - 109, f"@{self.handle}", 8.2, self.p.muted, width=width - 34)
        name = self.account.get("public_name") or f"@{self.handle}"
        # Names are public identity, not teaser copy. Fit all lines without an
        # ellipsis, including long names and names with one unbroken word.
        size = 13
        while len(self.lines(name, width - 34, size, bold=True)) * size * 1.3 > 71 and size > 6:
            size -= 0.5
        bottom = self.paragraph(x + 17, top - 132, name, width - 34,
                                size, self.p.title, leading=size * 1.3, bold=True)
        posts = self.account.get("profile_posts")
        if _number(posts) is not None:
            self.icon("posts", x + 17, bottom - 20, 12)
            self.text(x + 35, bottom - 16, f"{_fmt(posts, compact=True)} profile posts", 7.2, self.p.muted)
        url = _instagram_profile(self.handle)
        if url:
            self.rect(x + 17, top - height + 17, width - 34, 27, self.p.accent, radius=6)
            self.icon("posts", x + 28, top - height + 23, 13, self.p.accent_ink)
            self.text(x + 49, top - height + 27, "VIEW PUBLIC PROFILE", 6.8, self.p.accent_ink, bold=True)
            self.c.linkURL(url, (x, top - height, x + width, top), relative=0)

    def overview(self) -> None:
        self.editorial_page("Audience & performance")
        hero_top = H - 112
        profile_width = 191
        headline_width = CW - profile_width - 25
        profile_x = W - MARGIN - profile_width
        self.text(MARGIN, hero_top + 3, "THE AUDIENCE", 7.5, self.p.accent_text, bold=True)
        followers = _number(self.account.get("followers"))
        if followers is not None:
            self.display(MARGIN, hero_top - 73, _fmt(followers, compact=True), 77, width=headline_width)
            self.display(MARGIN, hero_top - 121, "FOLLOWERS.", 38, width=headline_width)
        else:
            self.display(MARGIN, hero_top - 63, "IN THE", 49, width=headline_width)
            self.display(MARGIN, hero_top - 121, "PUBLIC EYE.", 42, width=headline_width)
        self.display(MARGIN, hero_top - 168, "CONTENT THAT", 32, width=headline_width)
        self.display(MARGIN, hero_top - 209, "CONNECTS.", 36, self.p.accent_text, headline_width)
        self.profile_card(profile_x, hero_top + 12, profile_width, 275)
        bio = self.account.get("public_bio")
        introduction = bio or "Explore the public profile, content performance and standout posts from this Instagram account."
        self.paragraph(MARGIN, hero_top - 238, introduction, headline_width, 8.2,
                       self.p.muted, max_lines=3, leading=12)
        audience_y = 433
        growth = ((self.report.get("follower_growth") or {}).get("30d") or {}).get("pct")
        if _number(growth) is not None:
            change = _fmt(growth, "percent")
            if _number(growth) > 0:
                change = "+" + change
            self.text(MARGIN, audience_y, f"{change} audience change / last 30 days", 8.4, self.p.accent_text, bold=True)
        elif followers is not None:
            self.text(MARGIN, audience_y, f"{_fmt(followers)} followers on Instagram", 8.4, self.p.accent_text, bold=True)
        video_key = "video_views" if _number(_stat(self.all_time, "video_views", "average")) is not None else "video_plays"
        video_label = "video views" if video_key == "video_views" else "video plays"
        average_candidates = [
            ("Average likes", _stat(self.all_time, "likes", "average"), "count", "Per public post"),
            ("Average comments", _stat(self.all_time, "comments", "average"), "count", "Per public post"),
            (f"Average {video_label}", _stat(self.all_time, video_key, "average"), "count", "Per video with public counts"),
        ]
        averages = [item for item in average_candidates if _number(item[1]) is not None]
        self.display(MARGIN, 398, "CONTENT PERFORMANCE", 23)
        y = self.strip(averages, 380, number_size=31)
        if not averages:
            self.paragraph(MARGIN, 367, "Public performance highlights are not available yet.", CW, 10)
            y = 294
        total_candidates = [
            ("Total likes", _stat(self.all_time, "likes"), "count", "Across analyzed public posts"),
            (f"Total {video_label}", _stat(self.all_time, video_key), "count", "Across analyzed public videos"),
        ]
        totals = [item for item in total_candidates if _number(item[1]) is not None]
        if totals:
            y = self.strip(totals, y - 15, height=75, number_size=23)
        if self.recent_available:
            self.text(MARGIN, 189, "Posts published in the last 30 days", 11, self.p.title, bold=True)
            recent_video = "video_views" if _number(_stat(self.recent, "video_views")) is not None else "video_plays"
            recent_candidates = [
                ("Public posts", self.recent.get("post_count"), "count", ""),
                ("Likes", _stat(self.recent, "likes"), "count", ""),
                ("Comments", _stat(self.recent, "comments"), "count", ""),
                ("Video views" if recent_video == "video_views" else "Video plays", _stat(self.recent, recent_video), "count", ""),
            ]
            recent_items = [item for item in recent_candidates if _number(item[1]) is not None]
            self.strip(recent_items, 174, height=57, number_size=22)
            note = "Historical highlights summarize analyzed public posts. Recent figures cover posts published in the last 30 days and their current cumulative public counts. Video views and plays are separate measures; neither represents unique reach."
        else:
            self.text(MARGIN, 178, "A PUBLIC LOOK AT THE ACCOUNT", 7.3, self.p.accent_text, bold=True)
            self.paragraph(MARGIN, 161, "See the next page for selected posts and the public activity they generated.",
                           CW, 9, self.p.muted, max_lines=2)
            note = "Historical highlights summarize analyzed public posts and their current cumulative public counts. Video views and plays are separate measures; neither represents unique reach."
        self.paragraph(MARGIN, 94, note, CW, 6.7, self.p.muted, max_lines=3, leading=9.4)

    def portrait_post(self, post: dict[str, Any], x: float, top: float, width: float,
                      rank: int, height: float, *, compact: bool = False) -> None:
        self.rect(x, top - height, width, height, radius=9)
        self.text(x + 10, top - 17, f"{rank:02}", 8.5, self.p.accent_text, bold=True)
        format_name = post.get("format") if post.get("format") in ("Carousel", "Image", "Reel", "Video", "Post") else "Post"
        format_label = f"{_date(post['published_at'])} / {format_name}" if compact and post.get("published_at") else format_name.upper()
        self.text(x + width - 10, top - 17, format_label, 5.8 if compact else 6.2,
                  self.p.muted, width=width - 40, align="right")
        image_height = 133 if compact else 254
        image_top = top - 29
        self.rect(x + 9, image_top - image_height, width - 18, image_height, self.p.alternate, radius=4)
        if not self.image(post.get("thumbnail_bytes"), x + 9, image_top - image_height, width - 18, image_height):
            self.icon("play" if format_name in ("Video", "Reel") else "posts",
                      x + width / 2 - 19, image_top - image_height / 2 - 19, 38, self.p.muted)
            self.text(x + width / 2, image_top - image_height / 2 - 41,
                      "VIEW ON INSTAGRAM", 5.8, self.p.muted, align="center")
        metrics = post.get("metrics") or {}
        primary = next((key for key in ("video_views", "video_plays", "likes", "comments")
                        if _number(metrics.get(key)) is not None), None)
        primary_y = image_top - image_height - (25 if compact else 31)
        labels = {"likes": "likes", "comments": "comments", "video_views": "views", "video_plays": "plays"}
        if primary:
            self.display(x + 10, primary_y, _fmt(metrics[primary], compact=True),
                         27 if compact else 33, self.p.accent_text, width - 20)
            self.text(x + width - 10, primary_y + 2, labels[primary].upper(), 6.2,
                      self.p.muted, align="right")
        secondary = [key for key in ("likes", "comments", "video_views", "video_plays")
                     if key != primary and _number(metrics.get(key)) is not None]
        metric_y = primary_y - (18 if compact else 22)
        cell_width = (width - 20) / max(len(secondary), 1)
        for index, key in enumerate(secondary):
            at = x + 10 + index * cell_width
            self.icon(self.metric_icon(labels[key]), at, metric_y - 1, 10)
            self.text(at + 13, metric_y + 1, _fmt(metrics[key], compact=True), 7.2,
                      self.p.ink, bold=True, width=cell_width - 15)
            self.text(at + 13, metric_y - 9, labels[key], 5.8, self.p.muted, width=cell_width - 15)
        caption_y = metric_y - (19 if compact else 27)
        self.paragraph(x + 10, caption_y, post.get("public_caption") or "View this public post",
                       width - 20, 7.3 if compact else 8.5, self.p.ink,
                       max_lines=2 if compact else 7, leading=10 if compact else 12)
        if not compact:
            self.text(x + 10, top - height + 42, _date(post.get("published_at")), 6.5, self.p.muted)
        url = _instagram_post(post.get("permalink"))
        if url:
            self.link(x + 10, top - height + 16, "VIEW PUBLIC POST", url, size=6.4 if compact else 6.8)

    def strongest_posts(self) -> None:
        self.editorial_page("Content examples")
        self.text(MARGIN, H - 111, "CONTENT EXAMPLES", 7.5, self.p.accent_text, bold=True)
        self.display(MARGIN, H - 157, "CONTENT THAT CONNECTS.", 35, width=CW)
        introduction = "Selected public posts, their measured activity and a direct link to explore each one on Instagram."
        if self.recent_available:
            introduction += " Displayed public counts are cumulative since publication."
        self.paragraph(MARGIN, H - 180, introduction,
                       CW, 8.2, self.p.muted, max_lines=2, leading=12)
        groups = self.report.get("best_posts") or {}
        historical = (groups.get("all_time") or [])[:3]
        recent = (groups.get("last_30_days") or [])[:3] if self.recent_available else []
        width = (CW - 24) / 3
        if not self.recent_available:
            self.text(MARGIN, 626, "HISTORICAL HIGHLIGHTS", 7.4, self.p.accent_text, bold=True)
            for index, post in enumerate(historical):
                self.portrait_post(post, MARGIN + index * (width + 12), 609, width, index + 1, 501)
            if not historical:
                self.paragraph(MARGIN, 570, "Public post highlights are not available for this period.", CW, 10)
            note = "Highlights are selected by public likes and comments. Displayed public counts include cumulative activity since publication."
        else:
            top = 626
            for posts, title in ((historical, "HISTORICAL HIGHLIGHTS"), (recent, "PUBLISHED IN THE LAST 30 DAYS")):
                self.text(MARGIN, top, title, 7.4, self.p.accent_text, bold=True)
                for index, post in enumerate(posts):
                    self.portrait_post(post, MARGIN + index * (width + 12), top - 13,
                                       width, index + 1, 263, compact=True)
                if not posts:
                    message = "No public posts were published in this period." if posts is recent and self.recent.get("post_count") == 0 else "Public post highlights are not available for this period."
                    self.paragraph(MARGIN, top - 42, message, CW, 9)
                top -= 288
            note = "Highlights are selected by public likes and comments. Recent posts were published in the last 30 days; displayed counts include activity since publication."
        if not self.recent_available:
            self.paragraph(MARGIN, 85, note, CW, 6.7, self.p.muted, max_lines=2, leading=9.4)

    def finish(self) -> bytes:
        self.pages.append(dict(self.c.__dict__))
        total = len(self.pages)
        for index, state in enumerate(self.pages, start=1):
            self.c.__dict__.update(state)
            self.c.setStrokeColor(self.p.line)
            self.c.setLineWidth(0.6)
            self.c.line(MARGIN, 51, W - MARGIN, 51)
            prepared = f" / Prepared {_date(self.report['generated_at'])}" if self.report.get("generated_at") else ""
            self.text(MARGIN, 33, f"@{self.handle}{prepared} / Sentient", 6.5, self.p.muted, width=CW - 90)
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
