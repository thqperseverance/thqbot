"""Tiny SVG layout kit for the thqbot brand assets.

Goals:
  * deterministic geometry (no auto-layout surprises),
  * a *conservative* text-width estimator so overflow fails loudly at build
    time instead of silently on someone's screen,
  * a sidecar JSON of every text's allowed box, which check_svg.py verifies
    against real Chrome font metrics (getBBox).

Width model (deliberately pessimistic):
    CJK / fullwidth   -> 1.00 em
    ASCII             -> 0.58 em
    narrow (i l j t f . , : ; ' | ! )  -> 0.32 em
    space             -> 0.30 em
Inter's real averages are below these, so "fits" here means "fits for real".
"""

from __future__ import annotations

import json
from pathlib import Path

FONT_STACK = (
    'Inter, "Segoe UI", system-ui, -apple-system, "PingFang SC", '
    '"Microsoft YaHei", "Helvetica Neue", Arial, sans-serif'
)
MONO_STACK = '"SF Mono", "JetBrains Mono", "Fira Code", Consolas, "Liberation Mono", Menlo, monospace'

NARROW = set("iljtf.,:;'|!()[]{}`\"-")

# ------------------------------------------------------------------ palette
BG = "#0F0F11"
PANEL = "#1A1A1F"
LAYER2 = "#202027"
LAYER3 = "#26262E"
TEXT = "#EAEAEF"
MUTED = "#8A8A98"
DIM = "#6C6C7A"
ACCENT = "#679EFE"
ACCENT_SOFT = "rgba(103,158,254,0.14)"
ACCENT_LINE = "rgba(103,158,254,0.45)"
LINE1 = "rgba(255,255,255,0.06)"
LINE2 = "rgba(255,255,255,0.12)"
LINE3 = "rgba(255,255,255,0.16)"
OK_GREEN = "#22C55E"
WARN = "#F59E0B"

CJK_PUNCT = "\u00b7\u2014\u2026\u201c\u201d\u2018\u2019\u3001\u3002\uff0c\uff01\uff1f\uff1a\uff1b\uff08\uff09\u3010\u3011\u300a\u300b"


def esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def text_width(text: str, size: float, letter_spacing: float = 0.0) -> float:
    total = 0.0
    for ch in text:
        if ch == " ":
            total += 0.30
        elif ord(ch) > 0x2E7F or ch in CJK_PUNCT:
            total += 1.00
        elif ch in NARROW:
            total += 0.32
        else:
            total += 0.58
    return total * size + letter_spacing * max(0, len(text) - 1)


class Doc:
    """One SVG document plus the geometry manifest for verification."""

    def __init__(self, width: int, height: int, name: str, bg: str = BG):
        self.width = width
        self.height = height
        self.name = name
        self.bg = bg
        self.body: list[str] = []
        self.defs: list[str] = []
        self.manifest: list[dict] = []
        self._n = 0

    # ---------------------------------------------------------------- helpers
    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def define(self, markup: str) -> None:
        self.defs.append(markup)

    def add(self, markup: str) -> None:
        self.body.append(markup)

    # ------------------------------------------------------------------ shapes
    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        rx: float = 12,
        fill: str = PANEL,
        stroke: str | None = LINE1,
        sw: float = 1,
        opacity: float | None = None,
        dash: str | None = None,
    ) -> None:
        parts = [f'x="{x:g}"', f'y="{y:g}"', f'width="{w:g}"', f'height="{h:g}"', f'rx="{rx:g}"', f'fill="{fill}"']
        if stroke:
            parts += [f'stroke="{stroke}"', f'stroke-width="{sw:g}"']
        if opacity is not None:
            parts.append(f'opacity="{opacity:g}"')
        if dash:
            parts.append(f'stroke-dasharray="{dash}"')
        self.add(f"<rect {' '.join(parts)}/>")

    def circle(self, cx: float, cy: float, r: float, *, fill: str, opacity: float | None = None) -> None:
        extra = f' opacity="{opacity:g}"' if opacity is not None else ""
        self.add(f'<circle cx="{cx:g}" cy="{cy:g}" r="{r:g}" fill="{fill}"{extra}/>')

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        stroke: str = LINE1,
        sw: float = 1,
        dash: str | None = None,
        marker: str | None = None,
    ) -> None:
        parts = [f'x1="{x1:g}"', f'y1="{y1:g}"', f'x2="{x2:g}"', f'y2="{y2:g}"', f'stroke="{stroke}"', f'stroke-width="{sw:g}"']
        if dash:
            parts.append(f'stroke-dasharray="{dash}"')
        if marker:
            parts.append(f'marker-end="url(#{marker})"')
        self.add(f"<line {' '.join(parts)}/>")

    def path(self, d: str, *, stroke: str = LINE1, sw: float = 1, fill: str = "none", marker: str | None = None) -> None:
        parts = [f'd="{d}"', f'fill="{fill}"', f'stroke="{stroke}"', f'stroke-width="{sw:g}"']
        if marker:
            parts.append(f'marker-end="url(#{marker})"')
        self.add(f"<path {' '.join(parts)}/>")

    # -------------------------------------------------------------------- text
    def text(
        self,
        x: float,
        y: float,
        content: str,
        *,
        size: float = 16,
        weight: int = 400,
        fill: str = TEXT,
        anchor: str = "start",
        mono: bool = False,
        spacing: float = 0.0,
        opacity: float | None = None,
        allow: tuple[float, float, float, float] | None = None,
        max_w: float | None = None,
        check: bool = True,
    ) -> dict:
        """Draw text and register its allowed box.

        allow = (x0, y0, x1, y1) hard region the rendered glyphs must stay in.
        max_w additionally caps the rendered advance width.
        """
        est_w = text_width(content, size, spacing)
        if anchor == "middle":
            left = x - est_w / 2
        elif anchor == "end":
            left = x - est_w
        else:
            left = x
        top = y - size * 0.90
        bottom = y + size * 0.28
        right = left + est_w

        region = allow or (0.0, 0.0, float(self.width), float(self.height))
        if check:
            if left < region[0] - 0.5 or right > region[2] + 0.5:
                raise AssertionError(
                    f"[{self.name}] text overflows horizontally: {content!r} "
                    f"needs {est_w:.1f}px at x={left:.1f}..{right:.1f}, "
                    f"allowed {region[0]:.0f}..{region[2]:.0f}"
                )
            if top < region[1] - 0.5 or bottom > region[3] + 0.5:
                raise AssertionError(
                    f"[{self.name}] text overflows vertically: {content!r} "
                    f"at y={top:.1f}..{bottom:.1f}, allowed {region[1]:.0f}..{region[3]:.0f}"
                )
            if max_w is not None and est_w > max_w + 0.5:
                raise AssertionError(
                    f"[{self.name}] text wider than its box: {content!r} "
                    f"{est_w:.1f}px > {max_w:.1f}px"
                )

        tid = self._id("t")
        attrs = [
            f'x="{x:g}"',
            f'y="{y:g}"',
            f'font-size="{size:g}"',
            f'font-weight="{weight}"',
            f'fill="{fill}"',
        ]
        if anchor != "start":
            attrs.append(f'text-anchor="{anchor}"')
        if spacing:
            attrs.append(f'letter-spacing="{spacing:g}"')
        if opacity is not None:
            attrs.append(f'opacity="{opacity:g}"')
        if mono:
            attrs.append('class="mono"')
        attrs.append(f'id="{tid}"')
        self.add(f"<text {' '.join(attrs)}>{esc(content)}</text>")

        entry = {
            "id": tid,
            "text": content,
            "est_w": round(est_w, 2),
            "est_box": [round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)],
            "allow": [float(region[0]), float(region[1]), float(region[2]), float(region[3])],
        }
        if max_w is not None:
            entry["max_w"] = float(max_w)
        self.manifest.append(entry)
        return entry

    def block(
        self,
        x: float,
        y: float,
        lines: list[tuple[str, float]],
        *,
        size: float = 13,
        weight: int = 400,
        fill: str = MUTED,
        line_height: float = 20,
        allow: tuple[float, float, float, float] | None = None,
        max_w: float | None = None,
    ) -> None:
        for index, (line, _extra) in enumerate(lines):
            self.text(
                x,
                y + index * line_height,
                line,
                size=size,
                weight=weight,
                fill=fill,
                allow=allow,
                max_w=max_w,
            )

    # ------------------------------------------------------------------ widget
    def chip(
        self,
        x: float,
        y: float,
        label: str,
        *,
        size: float = 13,
        pad: float = 12,
        h: float = 30,
        fill: str = LAYER2,
        color: str = TEXT,
        stroke: str | None = None,
        mono: bool = False,
    ) -> float:
        """Left-anchored capsule chip; returns its width."""
        w = text_width(label, size) + pad * 2
        self.rect(x, y, w, h, rx=h / 2, fill=fill, stroke=stroke)
        self.text(x + pad, y + h / 2 + size * 0.35, label, size=size, fill=color, mono=mono, max_w=w - pad * 2)
        return w

    def icon_tile(self, x: float, y: float, size: float, glyph: str = "dot", *, fill: str = ACCENT_SOFT, color: str = ACCENT) -> None:
        self.rect(x, y, size, size, rx=size * 0.28, fill=fill, stroke=None)
        cx, cy = x + size / 2, y + size / 2
        if glyph == "dot":
            self.circle(cx, cy, size * 0.16, fill=color)
        elif glyph == "ring":
            self.add(
                f'<circle cx="{cx:g}" cy="{cy:g}" r="{size * 0.22:g}" fill="none" '
                f'stroke="{color}" stroke-width="2"/>'
            )
        elif glyph == "bars":
            for i, wf in enumerate((0.44, 0.30, 0.38)):
                yy = y + size * (0.34 + i * 0.16)
                self.add(
                    f'<rect x="{x + size * 0.28:g}" y="{yy:g}" width="{size * wf:g}" '
                    f'height="2.4" rx="1.2" fill="{color}"/>'
                )
        elif glyph == "bolt":
            self.path(
                f"M{cx + size * 0.10:g} {cy - size * 0.28:g} L{cx - size * 0.16:g} {cy + size * 0.03:g} "
                f"L{cx - size * 0.01:g} {cy + size * 0.03:g} L{cx - size * 0.10:g} {cy + size * 0.28:g} "
                f"L{cx + size * 0.17:g} {cy - size * 0.04:g} L{cx + size * 0.01:g} {cy - size * 0.04:g} Z",
                fill=color,
                stroke=color,
                sw=1,
            )
        elif glyph == "grid":
            for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                self.add(
                    f'<rect x="{cx - size * 0.22 + dx * size * 0.26:g}" '
                    f'y="{cy - size * 0.22 + dy * size * 0.26:g}" '
                    f'width="{size * 0.18:g}" height="{size * 0.18:g}" rx="2" fill="{color}"/>'
                )
        elif glyph == "lock":
            self.add(
                f'<rect x="{cx - size * 0.18:g}" y="{cy - size * 0.04:g}" width="{size * 0.36:g}" '
                f'height="{size * 0.26:g}" rx="3" fill="{color}"/>'
            )
            self.path(
                f"M{cx - size * 0.10:g} {cy - size * 0.04:g} v-{size * 0.10:g} "
                f"a{size * 0.10:g} {size * 0.10:g} 0 0 1 {size * 0.20:g} 0 v{size * 0.10:g}",
                stroke=color,
                sw=2,
            )

    def card(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        title: str,
        lines: list[str],
        glyph: str = "dot",
        rx: float = 12,
        fill: str = PANEL,
        stroke: str = LINE1,
        title_size: float = 16,
        body_size: float = 13,
        body_fill: str = MUTED,
        icon_size: float = 30,
        pad: float = 20,
        title_color: str = TEXT,
        tag: str | None = None,
        tag_color: str = ACCENT,
    ) -> None:
        self.rect(x, y, w, h, rx=rx, fill=fill, stroke=stroke)
        self.icon_tile(x + pad, y + pad, icon_size, glyph)
        inner_left = x + pad + icon_size + 12
        inner_right = x + w - pad
        self.text(
            inner_left,
            y + pad + icon_size * 0.68,
            title,
            size=title_size,
            weight=600,
            fill=title_color,
            allow=(inner_left, y + pad, inner_right, y + pad + icon_size),
            max_w=inner_right - inner_left,
        )
        if tag:
            self.text(
                x + w - pad,
                y + pad + icon_size * 0.68,
                tag,
                size=11,
                weight=500,
                fill=tag_color,
                anchor="end",
                allow=(x + w * 0.5, y + pad, inner_right, y + pad + icon_size),
            )
        body_top = y + pad + icon_size + 12
        for index, line in enumerate(lines):
            row_top = body_top + index * (body_size + 7)
            self.text(
                x + pad,
                row_top + body_size * 0.8,
                line,
                size=body_size,
                fill=body_fill,
                allow=(x + pad, row_top - body_size * 0.35, inner_right, row_top + (body_size + 7)),
                max_w=w - pad * 2,
            )

    # ------------------------------------------------------------------ chrome
    def title_block(self, x: float, y: float, title: str, subtitle: str, *, width: float) -> None:
        self.text(x, y, title, size=30, weight=700, fill=TEXT, max_w=width)
        self.text(x, y + 30, subtitle, size=14, fill=MUTED, max_w=width)

    # ------------------------------------------------------------------- output
    def save(self, path: Path, *, manifest_dir: Path | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        defs = "\n    ".join(self.defs)
        style = (
            "<style>\n"
            f"    text {{ font-family: {FONT_STACK}; }}\n"
            f"    .mono {{ font-family: {MONO_STACK}; }}\n"
            "  </style>"
        )
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}" role="img" aria-label="{esc(self.name)}">\n'
            f"  {style}\n"
            f"  <defs>\n    {defs}\n  </defs>\n"
            f'  <rect width="{self.width}" height="{self.height}" fill="{self.bg}"/>\n  '
            + "\n  ".join(self.body)
            + "\n</svg>\n"
        )
        path.write_text(svg, encoding="utf-8")
        if manifest_dir is not None:
            manifest_dir.mkdir(parents=True, exist_ok=True)
            (manifest_dir / f"{path.stem}.boxes.json").write_text(
                json.dumps(
                    {"svg": str(path), "width": self.width, "height": self.height, "texts": self.manifest},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        print(f"  wrote {path.name}  ({len(svg)} bytes, {len(self.manifest)} text runs)")


def glow_defs(doc: Doc) -> None:
    doc.define('''<radialGradient id="g-glow" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#679EFE" stop-opacity="0.20"/>
      <stop offset="0.55" stop-color="#679EFE" stop-opacity="0.06"/>
      <stop offset="1" stop-color="#679EFE" stop-opacity="0"/>
    </radialGradient>''')
    doc.define('''<linearGradient id="g-accent" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#8FBAFF"/>
      <stop offset="0.5" stop-color="#679EFE"/>
      <stop offset="1" stop-color="#3F6FE0"/>
    </linearGradient>''')
    doc.define('''<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 1.6 L9 5 L0 8.4 z" fill="#679EFE"/>
    </marker>''')
    doc.define('''<marker id="arm" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0 1.6 L9 5 L0 8.4 z" fill="#8A8A98"/>
    </marker>''')
    doc.define('''<marker id="arg" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 1.6 L9 5 L0 8.4 z" fill="#22C55E"/>
    </marker>''')


def mark(doc: Doc, x: float, y: float, size: float) -> None:
    """thqbot logomark: gradient rounded square, cut-out T, node dot."""
    s = size / 64.0
    doc.add(
        f'<g transform="translate({x:g} {y:g}) scale({s:g})">'
        f'<rect width="64" height="64" rx="18" fill="url(#g-accent)"/>'
        f'<path d="M17 23 H47" stroke="#0F0F11" stroke-width="7.5" stroke-linecap="round"/>'
        f'<path d="M32 23 V42" stroke="#0F0F11" stroke-width="7.5" stroke-linecap="round"/>'
        f'<circle cx="46.5" cy="45" r="5.2" fill="#0F0F11"/>'
        f"</g>"
    )
