#!/usr/bin/env python3
"""Generate PNG icons for the Mastisk Clipper extension.

Draws a rounded indigo square with a white "M" glyph.
Run with: uv run --with pillow python scripts/generate-icons.py
"""
import os
from PIL import Image, ImageDraw, ImageFont

INDIGO = (99, 102, 241, 255)   # #6366F1
INDIGO_DK = (79, 70, 229, 255)  # #4F46E5 (subtle bottom shade)
WHITE = (255, 255, 255, 255)


def _load_font(size):
    """Find a bold system font; fall back to Pillow's default."""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def create_icon(size):
    # Supersample for smooth edges, then downscale.
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = int(s * 0.04)
    radius = int(s * 0.22)
    draw.rounded_rectangle([pad, pad, s - pad - 1, s - pad - 1], radius=radius, fill=INDIGO)

    # Draw a centered bold "M".
    font = _load_font(int(s * 0.7))
    try:
        bbox = draw.textbbox((0, 0), "M", font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (s - tw) / 2 - bbox[0]
        y = (s - th) / 2 - bbox[1]
        draw.text((x, y), "M", font=font, fill=WHITE)
    except Exception:
        # Default bitmap font path — approximate centering.
        draw.text((s * 0.3, s * 0.28), "M", font=font, fill=WHITE)

    return img.resize((size, size), Image.LANCZOS)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    icons_dir = os.path.join(script_dir, "..", "icons")
    os.makedirs(icons_dir, exist_ok=True)

    for size in (16, 48, 128):
        icon = create_icon(size)
        path = os.path.join(icons_dir, f"icon{size}.png")
        icon.save(path)
        print(f"Created {path}")


if __name__ == "__main__":
    main()
