"""Generate Soundloom's Windows icon (icon.ico) from the brand mark.

Draws the same idea as frontend/img/logo.svg - amber weaving bars with a
white weft thread on a dark tile - with PIL, at every size an .ico needs.
Run once; the output is committed with the repo.
"""
from PIL import Image, ImageDraw

SIZES = [16, 24, 32, 48, 64, 128, 256]

ACCENT = (255, 180, 84, 255)      # --accent
ACCENT_DIM = (255, 180, 84, 150)
TILE = (22, 22, 28, 255)          # --bg-raise
TILE_EDGE = (51, 51, 61, 255)     # --border-strong
THREAD = (236, 236, 241, 255)     # --text


def draw_mark(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded dark tile filling the canvas.
    radius = max(2, size // 5)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=TILE,
                        outline=TILE_EDGE, width=max(1, size // 64))

    # Three amber bars of different heights (the "warp").
    pad = size * 0.16
    bar_w = size * 0.14
    gap = size * 0.05
    heights = [0.34, 0.52, 0.42]           # fraction of usable height
    top = pad
    usable = size - 2 * pad
    x = pad
    for h in heights:
        y0 = size - pad - usable * h
        d.rounded_rectangle([x, y0, x + bar_w, size - pad], radius=bar_w / 3,
                            fill=ACCENT if h != heights[1] else ACCENT_DIM)
        x += bar_w + gap

    # A white weft thread crossing the bars.
    y_mid = size * 0.52
    d.line([pad * 0.6, y_mid, size - pad * 0.6, size * 0.38], fill=THREAD,
           width=max(1, size // 22))

    return img


def main():
    import sys
    from pathlib import Path
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "icon.ico")
    frames = [draw_mark(s) for s in SIZES]
    frames[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
