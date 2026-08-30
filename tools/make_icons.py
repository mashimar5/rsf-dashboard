"""Regenerate static/apple-touch-icon.png.

iOS home-screen icons must be raster, so this cannot be the inline SVG the
browser tab uses. Run after changing the glyph:

    .venv/bin/pip install pillow
    .venv/bin/python tools/make_icons.py

Pillow is a build-time tool only and deliberately stays out of
requirements.txt -- the container serves the committed PNG.
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 180
SCALE = SIZE / 32          # the glyph is authored in a 32-unit box
BACKGROUND = "#2563eb"     # fixed, not occupancy-tinted: iOS caches the icon
GLYPH = "#ffffff"

# x, y, width, height, radius -- same dumbbell as the inline SVG favicon
BARS = [
    (5, 11, 4.5, 10, 1.6),
    (22.5, 11, 4.5, 10, 1.6),
    (9, 14.4, 14, 3.2, 1.6),
]


def main():
    # Square with no transparency and no corner rounding of its own: iOS
    # applies its own mask, and rounding here would show up doubled.
    image = Image.new("RGB", (SIZE, SIZE), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for x, y, width, height, radius in BARS:
        draw.rounded_rectangle(
            [x * SCALE, y * SCALE, (x + width) * SCALE, (y + height) * SCALE],
            radius=radius * SCALE,
            fill=GLYPH,
        )
    out = Path(__file__).parent.parent / "static" / "apple-touch-icon.png"
    image.save(out, "PNG")
    print(f"wrote {out} ({SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
