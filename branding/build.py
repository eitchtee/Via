"""Generate every Via icon from the logo source.

    uv run --no-project --with playwright python branding/build.py

Needs Google Chrome installed (used headless to rasterize the SVGs).

The source logo (branding/source/via-logo-*.svg) is the mark drawn edge to edge on a square
canvas, with no padding. Every variant here re-places the mark on its own canvas, sized for
where it's used: app tiles keep it well inside rounded corners, Android adaptive and PWA
maskable icons keep it inside the platform safe zone, and tiny sizes get a larger mark.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
SOURCE = ROOT / "source" / "via-logo-black.svg"
SVG_OUT = ROOT / "svg"
PNG_OUT = ROOT / "png"
WEB = REPO / "src" / "via" / "web"

TEAL = "#0e7c66"
WHITE = "#ffffff"
BLACK = "#000000"
CANVAS = 1024


def _mark() -> tuple[list[str], tuple[float, float, float, float]]:
    """The mark's <path> elements and its bounding box (x, y, w, h), strokes included.

    The box was measured once from the rendered source (getBBox with stroke) and is exact for
    the four round-capped strokes of the current logo.
    """
    svg = SOURCE.read_text(encoding="utf-8")
    paths = re.findall(r"<path [^>]*/>", svg)
    assert len(paths) == 4, "the logo is expected to have 4 strokes"
    return paths, (99.5547, 36.1992, 164.2383, 301.3867)


PATHS, (BX, BY, BW, BH) = _mark()
DIAGONAL = math.hypot(BW, BH)


def mark(color: str, *, height: float, canvas: int = CANVAS, weight: float = 1.0) -> str:
    """The mark, ``height`` (fraction of the canvas) tall, centered on the canvas.

    ``weight`` scales the stroke width (heavier strokes stay legible at tiny sizes).
    """
    k = height * canvas / BH
    cx, cy = BX + BW / 2, BY + BH / 2
    strokes = "".join(
        re.sub(r'stroke="#[0-9a-fA-F]{6}"', f'stroke="{color}"', p).replace(
            'stroke-width="33"', f'stroke-width="{33 * weight:g}"'
        )
        for p in PATHS
    )
    return (
        f'<g transform="translate({canvas / 2:g} {canvas / 2:g}) scale({k:.6f}) '
        f'translate({-cx:.4f} {-cy:.4f})">{strokes}</g>'
    )


def svg(body: str, canvas: int = CANVAS) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas} {canvas}" '
        f'width="{canvas}" height="{canvas}">{body}</svg>\n'
    )


def tile(*, radius: float, height: float, weight: float = 1.0) -> str:
    """Teal tile with the white mark. ``radius`` is a fraction of the size (0 = square)."""
    r = radius * CANVAS
    background = f'<rect width="{CANVAS}" height="{CANVAS}" rx="{r:g}" fill="{TEAL}"/>'
    return svg(background + mark(WHITE, height=height, weight=weight))


def safe_zone_height(circle: float) -> float:
    """Mark height (fraction of canvas) that fits its whole bounding box inside a centered
    circle of diameter ``circle`` (fraction of canvas)."""
    return circle * BH / DIAGONAL


VARIANTS: dict[str, str] = {
    # Rounded teal tile: favicons, browser extension, desktop apps, web UI logo.
    "app-icon": tile(radius=0.225, height=0.60),
    # Same, with a bigger, heavier mark so it stays legible at 16-48 px.
    "app-icon-small": tile(radius=0.2, height=0.72, weight=1.35),
    # Full-bleed square: iOS / App Store / Play Store listing and apple-touch-icon.
    # The platform rounds the corners itself, so the tile must be opaque and square.
    "app-icon-square": tile(radius=0, height=0.60),
    # PWA "maskable": full bleed, mark inside the 80% safe-zone circle (with margin).
    "maskable": tile(radius=0, height=round(safe_zone_height(0.80) * 0.85, 3)),
    # Android adaptive icon layers (108 dp canvas, 66 dp safe circle = 61%).
    "android-foreground": svg(mark(WHITE, height=round(safe_zone_height(0.61) * 0.92, 3))),
    "android-monochrome": svg(mark(BLACK, height=round(safe_zone_height(0.61) * 0.92, 3))),
    # Transparent mark with a little breathing room, for notification icons (white) and
    # anywhere a plain glyph is needed on a light (black) or dark (white) surface.
    "mark-white": svg(mark(WHITE, height=0.90)),
    "mark-black": svg(mark(BLACK, height=0.90)),
}

# Tight mark, cropped to its bounding box, for inline use (text lockups, headers).
TIGHT = (
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{BX:.4f} {BY:.4f} {BW:.4f} {BH:.4f}">'
    + "".join(re.sub(r'stroke="#[0-9a-fA-F]{6}"', 'stroke="currentColor"', p) for p in PATHS)
    + "</svg>\n"
)

PNGS: dict[str, list[int]] = {
    "app-icon": [64, 128, 192, 256, 512, 1024],
    "app-icon-small": [16, 32, 48],
    "app-icon-square": [180, 1024],
    "maskable": [512],
    "android-foreground": [432, 1024],
    "android-monochrome": [432, 1024],
    "mark-white": [24, 36, 48, 72, 96, 1024],  # 24 dp notification icon, mdpi…xxxhdpi
    "mark-black": [1024],
}

WEB_FILES = {
    "favicon.svg": ("app-icon-small", None),
    "icon.svg": ("app-icon", None),
    "icon-192.png": ("app-icon", 192),
    "icon-512.png": ("app-icon", 512),
    "maskable-512.png": ("maskable", 512),
    "apple-touch-icon.png": ("app-icon-square", 180),
}


def main() -> None:
    from playwright.sync_api import sync_playwright

    SVG_OUT.mkdir(exist_ok=True)
    PNG_OUT.mkdir(exist_ok=True)
    for name, text in VARIANTS.items():
        (SVG_OUT / f"{name}.svg").write_text(text, encoding="utf-8")
    (SVG_OUT / "mark.svg").write_text(TIGHT, encoding="utf-8")

    def render(page, name: str, size: int, target: Path) -> None:  # type: ignore[no-untyped-def]
        page.set_viewport_size({"width": size, "height": size})
        page.set_content(
            "<html><body style='margin:0;background:transparent'>"
            f"<img src='data:image/svg+xml;utf8,{VARIANTS[name].replace('#', '%23')}' "
            f"width='{size}' height='{size}' style='display:block'></body></html>"
        )
        page.wait_for_function("document.images[0].complete")
        page.screenshot(path=target, omit_background=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(device_scale_factor=1)
        for name, sizes in PNGS.items():
            for size in sizes:
                render(page, name, size, PNG_OUT / f"{name}-{size}.png")
        for filename, (name, size) in WEB_FILES.items():
            if size is None:
                (WEB / filename).write_text(VARIANTS[name], encoding="utf-8")
            else:
                render(page, name, size, WEB / filename)
        browser.close()
    print(
        f"wrote {len(VARIANTS) + 1} SVGs, {sum(map(len, PNGS.values()))} PNGs, "
        f"{len(WEB_FILES)} web files"
    )


if __name__ == "__main__":
    main()
