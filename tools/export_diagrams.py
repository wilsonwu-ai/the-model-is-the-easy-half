#!/usr/bin/env python3
"""Export every diagram HTML in diagrams/ to a PNG (for the README) and a
standalone SVG (for reuse).

PNG is what the README embeds: GitHub blocks external font loading inside SVGs
served from a repo, so an SVG would silently fall back to system fonts. The PNG
is rendered in a real browser where the webfonts do load.

Usage:  python3 tools/export_diagrams.py [--scale 2]
"""
import pathlib
import re
import subprocess
import sys
import xml.dom.minidom

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIAGRAMS = ROOT / "diagrams"
SVG_OUT = DIAGRAMS / "svg"
PNG_OUT = DIAGRAMS / "png"

FONT_IMPORT = (
    "<defs><style>@import url('https://fonts.googleapis.com/css2?"
    "family=Instrument+Serif:ital@0;1&amp;family=Geist:wght@400;500;600&amp;"
    "family=Geist+Mono:wght@400;500;600&amp;display=swap');</style></defs>"
)


def extract_svg(html_path):
    """Pull the first <svg>...</svg> block out of a diagram HTML file."""
    src = html_path.read_text(encoding="utf-8")
    match = re.search(r"<svg\b.*?</svg>", src, re.S)
    if not match:
        raise ValueError(f"no <svg> block in {html_path.name}")
    return match.group(0)


def write_svg(html_path):
    svg = extract_svg(html_path)
    if 'xmlns="http://www.w3.org/2000/svg"' not in svg:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    if "viewBox" not in svg:
        raise ValueError(f"{html_path.name}: no viewBox — refusing to guess one")

    # Merge the font @import into the existing <defs> rather than adding a second.
    if "<defs>" in svg:
        svg = svg.replace(
            "<defs>",
            "<defs><style>@import url('https://fonts.googleapis.com/css2?"
            "family=Instrument+Serif:ital@0;1&amp;family=Geist:wght@400;500;600&amp;"
            "family=Geist+Mono:wght@400;500;600&amp;display=swap');</style>",
            1,
        )
    else:
        svg = re.sub(r"(<svg\b[^>]*>)", r"\1" + FONT_IMPORT, svg, count=1)

    out = SVG_OUT / f"{html_path.stem}.svg"
    out.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + svg, encoding="utf-8")
    # Parse it back. A standalone .svg is strict XML; a bare & would break it.
    xml.dom.minidom.parseString(out.read_text(encoding="utf-8"))
    return out


RASTER = r'''
import pathlib, sys
from playwright.sync_api import sync_playwright
src, out, scale = sys.argv[1], sys.argv[2], float(sys.argv[3])
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(device_scale_factor=scale)
    pg.goto("file://" + str(pathlib.Path(src).resolve()))
    pg.wait_for_load_state("networkidle")
    pg.evaluate("document.fonts.ready")
    # Opaque background on purpose: a transparent PNG would show GitHub's dark
    # canvas through the light diagram's strokes.
    pg.locator("svg").first.screenshot(path=out)
    b.close()
'''


def write_png(html_path, scale):
    out = PNG_OUT / f"{html_path.stem}.png"
    script = ROOT / "tools" / "_raster.py"
    script.write_text(RASTER, encoding="utf-8")
    subprocess.run(
        [sys.executable, str(script), str(html_path), str(out), str(scale)],
        check=True, capture_output=True, text=True,
    )
    return out


def main():
    scale = 2.0
    if "--scale" in sys.argv:
        scale = float(sys.argv[sys.argv.index("--scale") + 1])

    SVG_OUT.mkdir(parents=True, exist_ok=True)
    PNG_OUT.mkdir(parents=True, exist_ok=True)

    files = sorted(DIAGRAMS.glob("*.html"))
    if not files:
        print("no diagram HTML found in diagrams/")
        return 1

    failures = []
    for html in files:
        try:
            svg = write_svg(html)
            png = write_png(html, scale)
            print(f"  ok  {html.name:32s} -> {svg.name}, {png.name}")
        except Exception as exc:  # noqa: BLE001 — report every file, fail at the end
            failures.append((html.name, exc))
            print(f"  FAIL {html.name:32s} {exc}")

    print(f"\n{len(files) - len(failures)}/{len(files)} exported at {scale}x")
    if failures:
        print("FAILURES:")
        for name, exc in failures:
            print(f"  {name}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
