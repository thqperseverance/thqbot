"""Export the poster/social card to PNG with Inter actually embedded.

GitHub's social preview must be a raster (PNG/JPG), and the committed SVGs
deliberately do NOT embed a 48 KB base64 font — so the export path injects
Inter at render time instead and keeps the SVG files small.
"""

from __future__ import annotations

import base64
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def browser() -> Path:
    for candidate in (CHROME, EDGE):
        if candidate.exists():
            return candidate
    raise SystemExit("no chrome/edge")


def find_inter(repo: Path) -> Path | None:
    hits = sorted((repo / "services/gateway/web/assets").glob("inter-latin-wght-normal-*.woff2"))
    return hits[0] if hits else None


def with_inter(svg_text: str, font: Path | None) -> str:
    if font is None:
        return svg_text
    data = base64.b64encode(font.read_bytes()).decode("ascii")
    face = (
        "<style>@font-face{font-family:'Inter';font-style:normal;font-weight:100 900;"
        f"src:url(data:font/woff2;base64,{data}) format('woff2');}}</style>"
    )
    # put the real font first in the stack
    return svg_text.replace("<style>", face + "<style>", 1)


def render(svg_path: Path, out: Path, font: Path | None, scale: float = 1.0) -> tuple[int, int]:
    svg_text = svg_path.read_text(encoding="utf-8")
    width = int(re.search(r'width="(\d+)"', svg_text).group(1))
    height = int(re.search(r'height="(\d+)"', svg_text).group(1))
    px_w, px_h = int(width * scale), int(height * scale)
    svg_text = with_inter(svg_text, font)
    svg_text = svg_text.replace(
        f'width="{width}" height="{height}"',
        f'width="{px_w}" height="{px_h}"',
        1,
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_svg = Path(tmp) / "a.svg"
        tmp_svg.write_text(svg_text, encoding="utf-8")
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<style>html,body{{margin:0;padding:0;background:#0F0F11;overflow:hidden}}"
            f"img{{display:block;width:{px_w}px;height:{px_h}px}}</style></head>"
            f"<body><img src='{tmp_svg.as_posix()}'></body></html>"
        )
        page = Path(tmp) / "w.html"
        page.write_text(html, encoding="utf-8")
        cmd = [
            str(browser()),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--virtual-time-budget=6000",
            f"--screenshot={out}",
            f"--window-size={px_w},{px_h}",
            page.as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if not out.exists():
            print(proc.stdout[-1500:])
            print(proc.stderr[-1500:])
            raise SystemExit(f"render failed: {svg_path.name}")
    print(f"  {svg_path.name} -> {out.name}  {px_w}x{px_h}  ({out.stat().st_size:,} bytes)")
    return px_w, px_h


def main() -> int:
    repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(".").resolve()
    docs = repo / "docs"
    assets = docs / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    font = find_inter(repo)
    print(f"inter font: {font.name if font else 'NOT FOUND (fallback stack)'}\n")

    render(docs / "poster.svg", docs / "poster.png", font)
    render(docs / "poster.svg", assets / "poster@2x.png", font, scale=2.0)
    render(docs / "social-preview.svg", docs / "social-preview.png", font)
    render(docs / "architecture-product.svg", docs / "architecture-product.png", font)
    render(docs / "architecture-tech.svg", docs / "architecture-tech.png", font)
    render(assets / "logo-wordmark.svg", assets / "logo-wordmark.png", font, scale=2.0)
    print("\npng exports done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
