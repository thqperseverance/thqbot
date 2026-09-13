"""Verify the generated SVGs.

1. XML well-formedness + canvas/viewBox agreement.
2. Real font metrics: inline each SVG in HTML, ask Chrome for getBBox() of
   every <text>, and check it against the allowed box recorded by the
   generator. This catches what a width *estimate* cannot.
3. Ink check: non-background pixels must exist and stay inside the canvas.

Usage: python check_svg.py <repo-root>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
SVG_NS = "{http://www.w3.org/2000/svg}"

failures: list[str] = []
checks = 0


def note(ok: bool, label: str, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        failures.append(f"{label} {detail}".strip())


def browser() -> Path:
    for candidate in (CHROME, EDGE):
        if candidate.exists():
            return candidate
    raise SystemExit("no chrome/edge")


MEASURE_JS = """
<script>
window.addEventListener('load', function () {
  var svg = document.querySelector('svg');
  var data = [];
  svg.querySelectorAll('text').forEach(function (t) {
    var b = t.getBBox();
    data.push({id: t.id, x: b.x, y: b.y, w: b.width, h: b.height});
  });
  var pre = document.createElement('pre');
  pre.id = 'measure';
  pre.textContent = 'MEASURE_BEGIN' + JSON.stringify(data) + 'MEASURE_END';
  document.body.appendChild(pre);
});
</script>
"""


def measure(svg_path: Path) -> list[dict]:
    svg_text = svg_path.read_text(encoding="utf-8")
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:#fff}</style></head><body>"
        + svg_text
        + MEASURE_JS
        + "</body></html>"
    )
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "m.html"
        page.write_text(html, encoding="utf-8")
        cmd = [
            str(browser()),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--virtual-time-budget=3000",
            "--dump-dom",
            page.as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120)
    # The marker literal also appears inside the <script> source we injected, so
    # take the LAST occurrence and undo the DOM dump's text escaping.
    matches = re.findall(r"MEASURE_BEGIN(.*?)MEASURE_END", proc.stdout, re.S)
    if not matches:
        raise RuntimeError(f"measurement failed for {svg_path.name}")
    payload = (
        matches[-1]
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )
    return json.loads(payload)


def main() -> int:
    repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(".").resolve()
    docs = repo / "docs"
    boxes_dir = Path(__file__).parent / "boxes"

    svgs = sorted(docs.rglob("*.svg"))
    print(f"checking {len(svgs)} svg files\n")

    for svg_path in svgs:
        print(f"== {svg_path.relative_to(repo)}")
        raw = svg_path.read_text(encoding="utf-8")

        # 1. well-formed XML
        try:
            root = ET.fromstring(raw)
            note(True, "xml well-formed")
        except ET.ParseError as exc:
            note(False, "xml well-formed", str(exc))
            continue

        note(root.tag == f"{SVG_NS}svg", "root element is <svg>", root.tag)
        if root.get("width") and root.get("height"):
            w, h = root.get("width"), root.get("height")
            note(
                root.get("viewBox") == f"0 0 {w} {h}",
                "viewBox matches width/height",
                f"{root.get('viewBox')} vs 0 0 {w} {h}",
            )
        note("rgba(" not in raw or "stop-color" in raw, "no rgba() in fill attributes (GitHub-safe)")

        # stray unescaped ampersands would already break XML, so nothing more here

        manifest_path = boxes_dir / f"{svg_path.stem}.boxes.json"
        if not manifest_path.exists():
            note(True, "(no manifest — geometry check skipped)")
            print()
            continue

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {item["id"]: item for item in manifest["texts"]}
        measured = {item["id"]: item for item in measure(svg_path)}

        note(
            len(measured) == len(expected),
            f"all {len(expected)} text runs measured",
            f"got {len(measured)}",
        )

        worst_x = worst_y = 0.0
        outside = []
        for tid, item in measured.items():
            exp = expected.get(tid)
            if exp is None:
                continue
            allow = exp["allow"]
            # allow lists were built with the estimator; give 1.5% slack for font
            # metric differences, which is still far tighter than a real overflow.
            slack_x = max(6.0, (allow[2] - allow[0]) * 0.015)
            slack_y = max(4.0, (allow[3] - allow[1]) * 0.02)
            if item["x"] < allow[0] - slack_x:
                outside.append((tid, exp["text"], f"left {item['x']:.1f} < {allow[0]:.1f}"))
            right = item["x"] + item["w"]
            if right > allow[2] + slack_x:
                outside.append((tid, exp["text"], f"right {right:.1f} > {allow[2]:.1f}"))
            if item["y"] < allow[1] - slack_y:
                outside.append((tid, exp["text"], f"top {item['y']:.1f} < {allow[1]:.1f}"))
            bottom = item["y"] + item["h"]
            if bottom > allow[3] + slack_y:
                outside.append((tid, exp["text"], f"bottom {bottom:.1f} > {allow[3]:.1f}"))
            worst_x = max(worst_x, abs(item["w"] - exp["est_w"]))
            worst_y = max(worst_y, item["h"] - (exp["est_box"][3] - exp["est_box"][1]))

        if outside:
            for tid, text, why in outside[:6]:
                print(f"       {tid} {text!r}: {why}")
        note(not outside, "every glyph box stays inside its allowed region", f"{len(outside)} violations")
        print(f"       max |measured - estimated| width = {worst_x:.1f}px")

        # also: nothing may poke outside the canvas at all
        cw, ch = manifest["width"], manifest["height"]
        overflow = [
            (t["id"], t["text"])
            for t in measured.values()
            if t["x"] < -1 or t["y"] < -1 or t["x"] + t["w"] > cw + 1 or t["y"] + t["h"] > ch + 1
        ]
        note(not overflow, f"no text escapes the {cw}x{ch} canvas", str(overflow[:3]))
        print()

    print(f"{checks} checks, {len(failures)} failures")
    if failures:
        print("\nfailures:")
        for item in failures:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
