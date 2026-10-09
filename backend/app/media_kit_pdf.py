"""Sales-ready PDF dashboards rendered entirely from a read-only account snapshot.

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


class _Report:
    def __init__(self, report: dict[str, Any], *, theme: str = "light", accent: str = DEFAULT_ACCENT):
        self.p = _make_palette(theme, accent)
        self.report = report
        self.account = report.get("account") or {}
        self.summary = report.get("summary") or {}
        self.all_time = self.summary.get("all_time") or {}
        self.recent = self.summary.get("last_30_days") or {}
        self.coverage = report.get("coverage") or {}
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
        self.c.setSubject("Account media kit - all stored metrics and coverage")
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
        self.text(W - MARGIN, H - 42, "ACCOUNT MEDIA KIT", 8, self.p.muted, align="right")
        self.text(MARGIN, H - 91, title, 23, self.p.title, bold=True, width=CW)
        self.paragraph(MARGIN, H - 109, subtitle, CW, 8.2, max_lines=2)
        self.y = H - 145

    def section(self, title: str, y: float) -> float:
        self.text(MARGIN, y, title, 13, self.p.title, bold=True)
        return y - 20

    def cards(self, items: list[tuple[str, Any, str, str]], y: float, columns: int = 3,
              height: float = 79) -> float:
        gap = 10
        width = (CW - (columns - 1) * gap) / columns
        for index, (label, value, unit, detail) in enumerate(items):
            col, row = index % columns, index // columns
            x, top = MARGIN + col * (width + gap), y - row * (height + gap)
            self.rect(x, top - height, width, height)
            self.text(x + 12, top - 19, label.upper(), 7.2, self.p.muted, bold=True, width=width - 24)
            self.text(x + 12, top - 43, _fmt(value, unit, compact=True), 22, self.p.title, bold=True, width=width - 24)
            self.text(x + 12, top - 63, detail, 6.7, self.p.muted, width=width - 24)
        return y - math.ceil(len(items) / columns) * (height + gap)

    def table(self, headers: list[str], rows: list[list[Any]], widths: list[float],
              y: float, size: float = 8, row_height: float = 27,
              x: float = MARGIN, page_title: str | None = None,
              subtitle: str = "", numeric_from: int = 1, bottom_y: float = 72,
              row_padding: float = 12) -> float:
        def head(at: float) -> float:
            self.rect(x, at - 25, sum(widths), 25, self.p.header, radius=4)
            offset = x
            for col, value in enumerate(headers):
                if col >= numeric_from:
                    self.text(offset + widths[col] - 8, at - 16, value, 6.5, self.p.header_ink,
                              bold=True, width=widths[col] - 14, align="right")
                else:
                    self.text(offset + 9, at - 16, value, 6.5, self.p.header_ink, bold=True, width=widths[col] - 15)
                offset += widths[col]
            return at - 25
        y = head(y)
        for index, row in enumerate(rows):
            line_count = max(len(self.lines(row[col], widths[col] - 18, size, bold=col == 0))
                             for col in range(min(numeric_from, len(row))))
            height = max(row_height, line_count * (size + 3) + row_padding)
            if y - height < bottom_y and page_title:
                self.page(page_title, subtitle)
                y = head(self.y)
            self.c.setFillColor(self.p.card if index % 2 == 0 else self.p.alternate)
            self.c.rect(x, y - height, sum(widths), height, fill=1, stroke=0)
            offset = x
            for col, value in enumerate(row):
                if col == 0:
                    self.paragraph(offset + 9, y - 16, value, widths[col] - 18,
                                   size, self.p.ink, leading=size + 3, bold=True)
                elif col >= numeric_from:
                    fitted_size = size
                    while fitted_size > 4.8 and pdfmetrics.stringWidth(_text(value), "MediaKit", fitted_size) > widths[col] - 14:
                        fitted_size -= 0.2
                    self.text(offset + widths[col] - 8, y - 16, value, fitted_size, self.p.ink, align="right")
                else:
                    self.paragraph(offset + 8, y - 16, value, widths[col] - 18, size, self.p.ink, leading=size + 3)
                offset += widths[col]
            y -= height
        return y

    def note(self, text: Any, y: float, height: float = 47) -> float:
        self.rect(MARGIN, y - height, CW, height, self.p.accent_soft)
        self.paragraph(MARGIN + 12, y - 16, text, CW - 24, 7.5, self.p.ink, max_lines=4)
        return y - height - 14

    def overview(self) -> None:
        name = self.account.get("name") or self.account.get("label") or f"@{self.handle}"
        self.page("The account at a glance", "An on-demand snapshot for sales conversations. Metrics reflect the data currently stored in Sentient.")
        top = self.y + 2
        self.rect(MARGIN, top - 136, CW, 136, self.p.hero, radius=12)
        avatar = self.image(self.account.get("avatar_bytes"), MARGIN + 20, top - 78, 59, 59)
        hero_x = MARGIN + (95 if avatar else 20)
        hero_width = CW - (115 if avatar else 40)
        self.text(hero_x, top - 32, name, 24, self.p.hero_ink, bold=True, width=hero_width)
        self.text(hero_x, top - 55, f"@{self.handle}  /  {self.account.get('platform') or 'Instagram'}", 11,
                  self.p.hero_muted, width=hero_width)
        profile = "  /  ".join(_text(value) for value in (self.account.get("group"), self.account.get("subcategory")) if value)
        self.text(hero_x, top - 76, profile or "Account performance profile", 8.2,
                  self.p.hero_muted, width=hero_width)
        bio = self.account.get("bio") or self.account.get("biography")
        if bio:
            self.paragraph(MARGIN + 20, top - 97, bio, CW - 40, 7.5,
                           self.p.hero_body, max_lines=2)
        else:
            self.text(MARGIN + 20, top - 101, "Profile captured: " + _date(self.account.get("profile_captured_at")),
                      8, self.p.hero_body)
        if _url(self.account.get("profile_url")):
            self.c.linkURL(self.account["profile_url"], (MARGIN, top - 136, W - MARGIN, top), relative=0)
        cards = [
            ("Followers", self.account.get("followers"), "count", "Latest stored profile snapshot"),
            ("Average likes", _stat(self.all_time, "likes", "average"), "count", "Per post with likes recorded"),
            ("Average video views", _stat(self.all_time, "video_views", "average"), "count", "Per post with views recorded"),
            ("Stored posts", self.all_time.get("post_count"), "count", "All stored history"),
            ("Total likes", _stat(self.all_time, "likes"), "count", "All stored history"),
            ("Total video views", _stat(self.all_time, "video_views"), "count", "All stored history"),
            ("Average comments", _stat(self.all_time, "comments", "average"), "count", "Per post with comments recorded"),
            ("Measured engagements", (self.all_time.get("engagements") or {}).get("total"), "count", "Known likes + comments; may be partial"),
            ("Engagement rate", self.all_time.get("engagement_rate_pct"), "percent", "Avg. engagements / current followers"),
        ]
        y = self.cards(cards, top - 155)
        self.text(MARGIN, y - 8, "Profile facts", 12, self.p.title, bold=True)
        facts = [
            ("Following", _fmt(self.account.get("following"))),
            ("Profile post count", _fmt(self.account.get("profile_posts"))),
            ("Verified", "Yes" if self.account.get("verified") is True else "No" if self.account.get("verified") is False else "N/A"),
            ("Profile visibility", "Private" if self.account.get("private") is True else "Public" if self.account.get("private") is False else "N/A"),
        ]
        width = CW / 4
        for i, (label, value) in enumerate(facts):
            self.text(MARGIN + i * width, y - 30, label, 7.3, self.p.muted)
            self.text(MARGIN + i * width, y - 49, value, 11, self.p.title, bold=True)
        self.note("Historical totals cover stored posts, not guaranteed lifetime totals. Views and plays are separate, and neither is unique reach. Engagements may be partial when either component is missing. N/A is unavailable; 0 is observed.", y - 69, 58)

    def follower_chart(self, x: float, y: float, width: float, height: float) -> None:
        history = [point for point in self.report.get("follower_history", []) if _number(point.get("followers")) is not None]
        self.rect(x, y - height, width, height)
        self.text(x + 15, y - 23, "Follower history", 11, self.p.title, bold=True)
        self.text(x + width - 15, y - 22, f"{len(history)} observations", 7, self.p.muted, align="right")
        if not history:
            self.paragraph(x + 15, y - 58, "Follower snapshots are not available yet. No growth estimate has been inferred.",
                           width - 30, 9, self.p.muted, max_lines=3)
            return
        values = [float(point["followers"]) for point in history]
        lower, upper = min(values), max(values)
        span = max(upper - lower, abs(upper) * 0.025, 1)
        lower -= span * 0.12
        upper += span * 0.12
        left, right = x + 54, x + width - 18
        bottom, top = y - height + 38, y - 49
        for i in range(4):
            value = lower + (upper - lower) * i / 3
            yy = bottom + (top - bottom) * i / 3
            self.c.setStrokeColor(self.p.line)
            self.c.setLineWidth(0.5)
            self.c.line(left, yy, right, yy)
            self.text(left - 9, yy - 2, _fmt(round(value)), 6.7, self.p.muted, align="right")
        times: list[float] = []
        for index, point in enumerate(history):
            try:
                stamp = datetime.fromisoformat(str(point.get("date")).replace("Z", "+00:00"))
                times.append((stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp())
            except (TypeError, ValueError, OverflowError):
                times.append(float(index))
        start, end = min(times), max(times)
        positions = [(left + (right - left) * ((stamp - start) / (end - start) if end != start else 0.5),
                      bottom + (top - bottom) * (value - lower) / (upper - lower))
                     for stamp, value in zip(times, values)]
        if len(positions) > 1:
            path = self.c.beginPath()
            path.moveTo(positions[0][0], bottom)
            for px, py in positions:
                path.lineTo(px, py)
            path.lineTo(positions[-1][0], bottom)
            path.close()
            self.c.setFillColor(self.p.accent_soft)
            self.c.drawPath(path, fill=1, stroke=0)
            path = self.c.beginPath()
            path.moveTo(*positions[0])
            for pos in positions[1:]:
                path.lineTo(*pos)
            outline = _contrast(self.p.values["accent"], self.p.values["card"]) < 3
            if outline:
                self.c.setStrokeColor(self.p.accent_text)
                self.c.setLineWidth(4.2)
                self.c.drawPath(path, stroke=1, fill=0)
            self.c.setStrokeColor(self.p.accent)
            self.c.setLineWidth(2.2)
            self.c.drawPath(path, stroke=1, fill=0)
        self.c.setFillColor(self.p.accent)
        outline = _contrast(self.p.values["accent"], self.p.values["card"]) < 3
        self.c.setStrokeColor(self.p.accent_text if outline else self.p.line)
        self.c.setLineWidth(0.6)
        self.c.circle(*positions[-1], 3, fill=1, stroke=int(outline))
        self.text(left, bottom - 17, _date(history[0].get("date")), 6.8, self.p.muted)
        self.text(right, bottom - 17, _date(history[-1].get("date")), 6.8, self.p.muted, align="right")

    def momentum(self) -> None:
        self.page("Audience & momentum", "Compare recent output with the historical baseline. Periods use the post publication date and stored metric values.")
        growth = self.report.get("follower_growth") or {}
        items = []
        for key, label in (("7d", "7-day follower change"), ("30d", "30-day follower change"), ("90d", "90-day follower change")):
            detail = growth.get(key) or {}
            items.append((label, detail.get("delta"), "count",
                          f"{_fmt(detail.get('pct'), 'percent')} / {_fmt(detail.get('observed_days'))} observed days"))
        y = self.cards(items, self.y, height=77)
        self.follower_chart(MARGIN, y - 2, CW, 206)
        y -= 233
        y = self.section("Performance by period", y)
        periods = [self.all_time, self.recent, self.summary.get("previous_30_days") or {}, self.summary.get("last_90_days") or {}]
        rows = [
            ["Posts", *[_fmt(period.get("post_count")) for period in periods]],
            ["Posts / week", *[_fmt(period.get("posts_per_week")) for period in periods]],
            ["Likes", *[_fmt(_stat(period, "likes"), compact=True) for period in periods]],
            ["Comments", *[_fmt(_stat(period, "comments"), compact=True) for period in periods]],
            ["Video views", *[_fmt(_stat(period, "video_views"), compact=True) for period in periods]],
            ["Video plays", *[_fmt(_stat(period, "video_plays"), compact=True) for period in periods]],
            ["Engagements", *[_fmt((period.get("engagements") or {}).get("total"), compact=True) for period in periods]],
            ["Engagement rate", *[_fmt(period.get("engagement_rate_pct"), "percent") for period in periods]],
        ]
        y = self.table(["Metric", "Stored history", "Last 30d", "Prior 30d", "Last 90d"], rows,
                       [137, 104, 88, 91, CW - 420], y, row_height=24)
        self.note("Growth uses actual snapshot endpoints and may cover a different number of days than the selected window. Engagement rates use current followers and measured likes + comments; rates are benchmarks, not campaign attribution.", y - 13, 58)

    def bars(self, entries: list[dict[str, Any]], x: float, y: float, width: float, height: float,
             title: str, limit: int = 7) -> None:
        self.rect(x, y - height, width, height)
        self.text(x + 13, y - 22, title, 10.5, self.p.title, bold=True)
        rows = entries[:limit]
        if not rows:
            self.text(x + 13, y - 54, "No observations available", 8, self.p.muted)
            return
        max_value = max((_number(row.get("post_count")) or 0 for row in rows), default=1) or 1
        row_height = min(30, (height - 45) / len(rows))
        for i, row in enumerate(rows):
            top = y - 43 - i * row_height
            self.text(x + 13, top, row.get("label") or "Unknown", 7.4, self.p.ink, width=width - 66)
            self.text(x + width - 13, top, _fmt(row.get("post_count")), 7.4, self.p.muted, align="right")
            self.rect(x + 13, top - 12, width - 26, 5, self.p.accent_soft, radius=2)
            value = _number(row.get("post_count")) or 0
            if value:
                self.rect(x + 13, top - 12, (width - 26) * value / max_value, 5, self.p.accent, radius=2)

    def content(self) -> None:
        self.page("The content profile", "Understand the account's creative mix, publishing rhythm and the source material behind its performance.")
        breakdowns = self.report.get("breakdowns") or {}
        gap = 13
        box_width = (CW - gap) / 2
        self.bars(breakdowns.get("formats") or [], MARGIN, self.y, box_width, 188, "Content formats")
        self.bars(breakdowns.get("weekdays") or [], MARGIN + box_width + gap, self.y, box_width, 188, "Publishing by weekday")
        y = self.section("Format performance", self.y - 211)
        rows = [[row.get("label") or "Unknown", _fmt(row.get("post_count")), _fmt(row.get("share_pct"), "percent"),
                 _fmt(_stat(row, "likes", "average"), compact=True), _fmt(_stat(row, "video_views", "average"), compact=True)]
                for row in breakdowns.get("formats") or []]
        y = self.table(["Format", "Posts", "Share", "Avg. likes", "Avg. views"], rows or [["Not available", "N/A", "N/A", "N/A", "N/A"]],
                       [163, 66, 75, 106, CW - 410], y, row_height=27,
                       page_title="The content profile", subtitle="Format performance continued.")
        content = self.report.get("content") or {}

        def tokens(entries: Any) -> str:
            if isinstance(entries, dict):
                entries = [{"label": key, "count": value} for key, value in entries.items()]
            output = []
            for entry in entries[:9]:
                if isinstance(entry, dict):
                    label = entry.get("label") or entry.get("tag") or entry.get("name") or entry.get("value") or entry.get("username")
                    count = entry.get("count", entry.get("post_count"))
                    output.append(f"{label} ({_fmt(count)})" if count is not None else str(label))
                else:
                    output.append(str(entry))
            return ", ".join(output) or "Not available"

        signals = [(label, tokens(entries)) for label, entries in
                   (("Top hashtags", content.get("hashtags") or []), ("Tagged accounts", content.get("mentions") or []),
                    ("Collaborators", content.get("coauthors") or []), ("Music / audio", content.get("music") or []))]
        signal_height = sum(min(3, len(self.lines(value, CW - 121, 8))) * 12 + 8 for _, value in signals)
        # Reserve the note and footer before placing the whole creative block.
        # This prevents a rich account from getting a page with only the note.
        if y - 47 - signal_height < 132:
            self.page("The content profile", "Creative signals and their source coverage.")
            y = self.section("Creative signals", self.y)
        else:
            y = self.section("Creative signals", y - 27)
        for label, value in signals:
            self.text(MARGIN, y, label, 8.5, self.p.title, bold=True)
            y = self.paragraph(MARGIN + 121, y, value, CW - 121, 8, self.p.muted, max_lines=3) - 8
        self.note("Publishing times reflect recorded publication timestamps in the report timezone. Format comparisons use observed values; the complete metric overview follows.", y - 3, 57)

    def post_card(self, post: dict[str, Any] | None, x: float, y: float, width: float, rank: int) -> None:
        self.rect(x, y - 81, width, 81)
        if not post:
            self.text(x + 12, y - 29, "No eligible post recorded", 8, self.p.muted)
            return
        self.text(x + 12, y - 17, f"{rank:02}", 8.3, self.p.accent_text, bold=True)
        self.text(x + width - 12, y - 17, f"{_date(post.get('published_at'))} / {post.get('format') or 'Post'}",
                  6.4, self.p.muted, width=width - 46, align="right")
        caption = post.get("hook_text") or post.get("caption") or post.get("shortcode") or "Untitled post"
        has_image = self.image(post.get("thumbnail_bytes"), x + 12, y - 51, 32, 28)
        self.paragraph(x + (52 if has_image else 12), y - 32, caption, width - (64 if has_image else 24),
                       7.5, self.p.title, max_lines=2, leading=10, bold=True)
        metrics = post.get("metrics") or {}
        parts = [f"{_fmt(metrics.get('likes'), compact=True)} likes", f"{_fmt(metrics.get('comments'), compact=True)} comments"]
        if metrics.get("video_views") is not None:
            parts.append(f"{_fmt(metrics.get('video_views'), compact=True)} views")
        elif metrics.get("video_plays") is not None:
            parts.append(f"{_fmt(metrics.get('video_plays'), compact=True)} plays")
        self.text(x + 12, y - 61, " / ".join(parts), 6.5, self.p.muted, width=width - 24)
        self.link(x + 12, y - 74, "VIEW POST", post.get("permalink"), size=6.1)
        completeness = "partial " if post.get("engagement_complete") is False else ""
        rank_metric = post.get("rank_metric")
        if rank_metric and rank_metric != "engagements":
            rank_label = {"video_views": "views", "video_plays": "plays"}.get(rank_metric, str(rank_metric).replace("_", " "))
            footer = f"{_fmt(post.get('rank_value'), compact=True)} {rank_label}"
        else:
            footer = f"{_fmt(post.get('engagements'), compact=True)} {completeness}engagements"
        self.text(x + width - 12, y - 74, footer, 6.1, self.p.muted, align="right")

    def strongest_posts(self) -> None:
        self.page("Posts that prove performance", "The strongest recorded examples, ranked by recorded likes + comments. Click VIEW POST to open the original content.")
        groups = self.report.get("best_posts") or {}
        all_posts = groups.get("all_time") or []
        recent_posts = groups.get("last_30_days") or []
        width = (CW - 13) / 2
        self.text(MARGIN, self.y, "ALL STORED HISTORY", 8, self.p.accent_text, bold=True)
        self.text(MARGIN + width + 13, self.y, "PUBLISHED IN THE LAST 30 DAYS", 8, self.p.accent_text, bold=True)
        y = self.y - 13
        for index in range(max(len(all_posts), len(recent_posts), 1)):
            if y - 81 < 127:
                self.page("Posts that prove performance", "Strongest posts continued. Rankings use recorded likes + comments.")
                y = self.y
            self.post_card(all_posts[index] if index < len(all_posts) else None, MARGIN, y, width, index + 1)
            self.post_card(recent_posts[index] if index < len(recent_posts) else None, MARGIN + width + 13, y, width, index + 1)
            y -= 90
        self.note("The recent list uses publication dates in the last 30 days. It reports current stored metrics for those posts, not views or engagements earned exclusively during that window. Rankings do not establish paid campaign results.", y - 4, 57)

    def champions(self) -> None:
        groups = (self.report.get("best_posts") or {}).get("by_metric") or {}
        if not any((group or {}).get("all_time") for group in groups.values()):
            return
        self.page("Standouts by metric", "Different strengths matter in different sales conversations. Each example leads its recorded metric; posts with no measurement are excluded.")
        y = self.y
        width = (CW - 13) / 2
        labels = {"likes": "Likes", "comments": "Comments", "video_views": "Video views", "video_plays": "Video plays"}
        for key, group in groups.items():
            group = group or {}
            if not group.get("all_time") and not group.get("last_30_days"):
                continue
            if y < 185:
                self.page("Standouts by metric", "Metric leaders continued. Left: stored history. Right: published in the last 30 days.")
                y = self.y
            self.text(MARGIN, y, f"{labels.get(key, key).upper()} / STORED HISTORY", 7.3, self.p.accent_text, bold=True)
            self.text(MARGIN + width + 13, y, f"{labels.get(key, key).upper()} / LAST 30 DAYS", 7.3, self.p.accent_text, bold=True)
            y -= 13
            self.post_card((group.get("all_time") or [None])[0], MARGIN, y, width, 1)
            self.post_card((group.get("last_30_days") or [None])[0], MARGIN + width + 13, y, width, 1)
            y -= 103
        trends = self.report.get("trends_pct") or {}
        if trends:
            if y < 195:
                self.page("Recent performance trends", "Average observed post metrics: last 30-day publication cohort compared with the prior 30-day cohort.")
                y = self.y
            y = self.section("Recent average performance change", y)
            self.table(["Metric", "Last 30d vs. prior 30d"], [[labels.get(key, key), _fmt(value, "percent")] for key, value in trends.items()],
                       [CW - 185, 185], y, row_height=24)

    def business_summary(self, y: float) -> float:
        """Keep the known sales profile concise, without raw demographic rows."""
        def brief(value: Any, depth: int = 0) -> str:
            if isinstance(value, str) and value[:1] in ("{", "["):
                import json
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            if isinstance(value, dict):
                entries = list(value.items())
                if entries and all(_number(child) is not None for _, child in entries):
                    entries.sort(key=lambda item: float(item[1]), reverse=True)
                return "; ".join(f"{str(key).replace('_', ' ')}: {brief(child, depth + 1)}"
                                 for key, child in entries[:5])
            if isinstance(value, list):
                return "; ".join(brief(child, depth + 1) for child in value[:5])
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return _fmt(value)
            return _text(value)

        details = []
        contact = " / ".join(_text(self.account[key]) for key in ("email", "phone")
                             if self.account.get(key))
        location = " / ".join(brief(self.account[key]) for key in ("city", "country", "business_address")
                              if self.account.get(key))
        category = " / ".join(_text(self.account[key]) for key in ("business_category", "language")
                              if self.account.get(key))
        for label, value in (("Contact", contact), ("Website", self.account.get("website")),
                             ("Location", location), ("Category / language", category),
                             ("Biography", self.account.get("bio") or self.account.get("biography")),
                             ("Audience highlights", self.account.get("demographics"))):
            if value:
                details.append((label, brief(value)))
        if not details:
            return y
        height = 33 + sum(min(3, len(self.lines(value, CW - 158, 8))) * 12 + 9
                          for _, value in details)
        if y - height < 152:
            self.page("Business & audience overview", "Known contact and audience highlights for sales conversations. Audience details are provider-reported; leading cohorts are summarized.")
            y = self.y
        else:
            y -= 24
        self.rect(MARGIN, y - height, CW, height)
        self.text(MARGIN + 13, y - 21, "Business & audience profile", 11, self.p.title, bold=True)
        at = y - 45
        for label, value in details:
            self.text(MARGIN + 13, at, label, 7.7, self.p.title, bold=True, width=130)
            at = self.paragraph(MARGIN + 145, at, value, CW - 158, 8, self.p.muted,
                                max_lines=3, leading=12) - 9
        return y - height - 15

    def compact_metrics(self) -> None:
        """Show every observed metric type once, without distributing raw rows."""
        catalog = {entry.get("key"): entry for entry in self.report.get("metric_catalog", [])}
        entries = [entry for entry in self.report.get("metrics_appendix") or []
                   if (entry.get("all_time") or {}).get("count")
                   and not str(entry.get("key") or "").endswith("_measured_slides")]
        if (self.all_time.get("engagements") or {}).get("count"):
            entries.append({"key": "measured_engagements", "label": "Measured engagements", "unit": "count",
                            "all_time": self.all_time["engagements"],
                            "last_30_days": self.recent.get("engagements") or {}})
        self.page("The complete metric overview",
                  "Every observed metric type, summarized once. Historical totals cover stored posts; recent values cover posts published in the last 30 days.")
        rows = []
        for entry in entries:
            key = str(entry.get("key") or "")
            unit = str(entry.get("unit") or (catalog.get(key) or {}).get("unit") or "count")
            label = str(entry.get("label") or key.replace("_", " ").title()).replace(" (separate from parent)", "")
            if unit == "model_score" and not label.lower().startswith("model"):
                label = "Model " + label
            display_unit = {"model_score": "model signal", "provider_value": "provider units"}.get(unit, unit)
            if key == "video_duration":
                display_unit = "seconds"
            elif key == "carousel_slide_video_duration":
                display_unit = "seconds"
            total_is_meaningful = unit not in ("ratio", "percent", "model_score")
            values = []
            for period_key in ("all_time", "last_30_days"):
                stats = entry.get(period_key) or {}
                total = _fmt(stats.get("total"), unit, compact=True) if total_is_meaningful else "-"
                average = _fmt(stats.get("average"), unit, compact=unit == "count")
                values.extend((total, average))
            rows.append([label, display_unit, *values])
        if rows:
            y = self.table(["Metric", "Unit", "History total", "History avg.", "30d total", "30d avg."],
                           rows, [173, 86, 72, 63, 63, CW - 457], self.y, size=7, row_height=18,
                           page_title="The complete metric overview",
                           subtitle="Observed metric types continued. Ratios, percentages and model signals are averages, with no additive total.",
                           numeric_from=2, bottom_y=157, row_padding=8)
        else:
            y = self.paragraph(MARGIN, self.y - 8,
                               "No post metrics have been observed for this account yet. Core unavailable metrics remain N/A.",
                               CW, 10) - 12
        y = self.business_summary(y)
        # Provenance is a single integrated strip, rather than a separate
        # source ledger. It stays above the footer on the final overview page.
        source = (
            f"DATA SCOPE / {_fmt(self.coverage.get('post_count', self.all_time.get('post_count')))} stored posts; "
            f"{_date(self.coverage.get('oldest_post_at'))} to {_date(self.coverage.get('newest_post_at'))}. "
            f"Updated: {_date(self.coverage.get('last_metrics_update_at'), with_time=True)}. "
            "Averages use observed inputs; 0 is measured, N/A is unknown. "
            "Model signals are estimates. Child metrics stay separate from parent posts; "
            "their averages use summed measured slides per carousel."
        )
        helpers = [entry for entry in self.report.get("metrics_appendix") or []
                   if str(entry.get("key") or "").endswith("_measured_slides")
                   and _number((entry.get("all_time") or {}).get("total"))]
        if helpers:
            parts = []
            for entry in helpers:
                label = str(entry["key"]).removeprefix("carousel_slide_").removesuffix("_measured_slides").replace("_", " ")
                label = label.removeprefix("video ")
                parts.append(f"{label}: {_fmt(entry['all_time']['total'])} slides")
            source += " Observed child inputs: " + "; ".join(parts) + "."
        self.note(source, min(y - 7, 146), height=77)

    def finish(self) -> bytes:
        self.pages.append(dict(self.c.__dict__))
        total = len(self.pages)
        for index, state in enumerate(self.pages, start=1):
            self.c.__dict__.update(state)
            self.c.setStrokeColor(self.p.line)
            self.c.setLineWidth(0.6)
            self.c.line(MARGIN, 51, W - MARGIN, 51)
            self.text(MARGIN, 33, f"@{self.handle} / Generated {self.generated}", 6.5, self.p.muted, width=CW - 90)
            self.text(W - MARGIN, 33, f"{index:02} / {total:02}", 6.5, self.p.muted, align="right")
            self.c.showPage()
        self.c.save()
        return self.stream.getvalue()


def render_media_kit_pdf(report: dict[str, Any], *, theme: str = "light", accent: str = DEFAULT_ACCENT) -> bytes:
    """Return a complete, paginated account media-kit PDF as bytes."""
    document = _Report(report, theme=theme, accent=accent)
    document.overview()
    document.momentum()
    document.content()
    document.strongest_posts()
    document.champions()
    document.compact_metrics()
    return document.finish()
