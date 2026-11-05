"""Draws the RS3 Desk icon: Hermes' wings on a stack of RS3 gold coins, in a GE-style frame.

  python tools/make_icon.py    writes console/icon.ico (16–256 px) and console/icon.png (256 px)
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "console"
S = 1024  # draw big, scale down for crisp edges

BG_TOP, BG_BOT = (58, 44, 30), (24, 18, 12)       # GE panel brown
FRAME = (201, 160, 72)                            # RS gold trim
COIN, COIN_DARK, COIN_LIGHT = (240, 190, 50), (168, 118, 20), (255, 230, 130)
WING, WING_EDGE = (250, 250, 246), (150, 160, 175)


def rounded_bg() -> Image.Image:
    grad = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        d.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOT)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([16, 16, S - 16, S - 16], radius=200, fill=255)
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    im.paste(grad, (0, 0), mask)
    ImageDraw.Draw(im).rounded_rectangle([28, 28, S - 28, S - 28], radius=190, outline=FRAME, width=34)
    return im


def coin(d: ImageDraw.ImageDraw, cx: int, cy: int, w: int, h: int, edge: int):
    """One coin seen at an angle: dark rim (thickness), then the gold face."""
    d.ellipse([cx - w, cy - h + edge, cx + w, cy + h + edge], fill=COIN_DARK)
    d.rectangle([cx - w, cy, cx + w, cy + edge], fill=COIN_DARK)
    d.ellipse([cx - w, cy - h, cx + w, cy + h], fill=COIN)
    d.ellipse([cx - w * 0.72, cy - h * 0.62, cx + w * 0.72, cy + h * 0.62], outline=COIN_LIGHT, width=10)


def wing(side: int) -> Image.Image:
    """Hermes wing made of layered feathers; side = -1 left, +1 right."""
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    root = (S // 2 + side * 150, 600)
    # three rows of feathers, swept up like Hermes' winged sandals; kept inside the frame
    for row, (length, rise, count) in enumerate(((250, 380, 5), (200, 290, 4), (140, 200, 3))):
        for i in range(count):
            t = i / max(count - 1, 1)
            tip_x = root[0] + side * (length * (0.55 + 0.45 * t))
            tip_y = root[1] - rise * (1 - t * 0.6) - 40 + row * 40
            base_y = root[1] + 30 - row * 10 + i * 6
            w = 46 - row * 6
            poly = [(root[0], base_y - w), (tip_x, tip_y), (tip_x + side * 10, tip_y + 40),
                    (root[0], base_y + w)]
            d.polygon(poly, fill=WING, outline=WING_EDGE, width=6)
    return im


def draw() -> Image.Image:
    im = rounded_bg()
    for side in (-1, 1):
        im.alpha_composite(wing(side))
    d = ImageDraw.Draw(im)
    cx = S // 2
    for i, cy in enumerate((780, 670, 560)):  # bottom to top
        coin(d, cx, cy, 230, 92, 50)
    # glow so the coins read at 16 px
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - 260, 460, cx + 260, 660], fill=(255, 220, 120, 70))
    im.alpha_composite(glow.filter(ImageFilter.GaussianBlur(30)))
    return im


def main():
    im = draw()
    OUT.mkdir(exist_ok=True)
    im.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png")
    im.resize((256, 256), Image.LANCZOS).save(OUT / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("wrote", OUT / "icon.ico")


if __name__ == "__main__":
    main()
