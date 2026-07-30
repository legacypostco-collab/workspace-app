#!/usr/bin/env python3
"""Генератор og-card.png — визитка для превью ссылки в Telegram/соцсетях.

ЧБ-градиент, чистый векторный логотип из logo-icon-white.svg,
название «CONSOLIDATOR PARTS» одним белым цветом, читаемые тёмные плашки.
1200×630 (стандарт og:image).

Запуск:  /opt/homebrew/bin/python3.14 static/brand/gen_og_card.py
"""
import os
import re
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "og-card.png")
SVG = os.path.join(HERE, "logo-icon-white.svg")

W, H = 1200, 630
FONT_BOLD = next(path for path in (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
) if os.path.exists(path))
FONT_REG = next(path for path in (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
) if os.path.exists(path))

# ---- размеры (чуть крупнее лого + название) ----
LOGO = 122            # было 100
NAME_PX = 45          # было 38
MARGIN = 72


def load_polygons():
    txt = open(SVG, encoding="utf-8").read()
    m = re.search(r'viewBox="([\d.\s]+)"', txt)
    vb = [float(x) for x in m.group(1).split()]
    vw, vh = vb[2], vb[3]
    polys = []
    for pts in re.findall(r'points="([\d.\s]+)"', txt):
        nums = [float(x) for x in pts.split()]
        polys.append(list(zip(nums[0::2], nums[1::2])))
    return polys, vw, vh


def draw_logo(canvas, x, y, size, color=(245, 245, 245, 255)):
    """Рисуем логотип из SVG-полигонов: рендер в 8× и down-scale для сглаживания."""
    SS = 8
    s = size * SS
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    polys, vw, vh = load_polygons()
    sx, sy = s / vw, s / vh
    for poly in polys:
        d.polygon([(px * sx, py * sy) for px, py in poly], fill=color)
    layer = layer.resize((size, size), Image.LANCZOS)
    canvas.alpha_composite(layer, (x, y))


def gradient_bg():
    """Диагональный ЧБ-градиент + лёгкий спотлайт + виньетка."""
    import numpy as np
    yy, xx = np.mgrid[0:H, 0:W].astype("float32")
    # диагональ от верх-лево (светлее) к низ-право (темнее)
    t = (xx / W * 0.55 + yy / H * 0.45)
    base = 38 - t * 26           # 38 → ~12
    # мягкий спотлайт слева-сверху за логотипом
    cx, cy = W * 0.18, H * 0.22
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    spot = np.clip(1 - r / (W * 0.55), 0, 1) ** 2 * 18
    val = np.clip(base + spot, 6, 60)
    # виньетка по углам
    vr = np.sqrt((xx - W / 2) ** 2 + (yy - H / 2) ** 2)
    vig = np.clip(1 - vr / (W * 0.72), 0, 1) ** 1.5
    val = val * (0.55 + 0.45 * vig)
    arr = np.clip(val, 0, 255).astype("uint8")
    rgb = np.dstack([arr, arr, arr])
    return Image.fromarray(rgb, "RGB").convert("RGBA")


def faint_emblem(canvas):
    """Призрачная крупная эмблема справа — фактура, едва видна."""
    polys, vw, vh = load_polygons()
    size = 520
    SS = 4
    s = size * SS
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    sx, sy = s / vw, s / vh
    for poly in polys:
        d.polygon([(px * sx, py * sy) for px, py in poly], fill=(255, 255, 255, 16))
    layer = layer.resize((size, size), Image.LANCZOS)
    canvas.alpha_composite(layer, (W - size + 70, H - size + 40))


def rounded_pill(draw, x, y, text, font, pad_x=24, pad_y=13):
    tb = draw.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    w = tw + pad_x * 2
    h = th + pad_y * 2
    r = h // 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r,
                           fill=(18, 18, 20, 235), outline=(255, 255, 255, 92), width=2)
    draw.text((x + pad_x - tb[0], y + pad_y - tb[1]), text,
              font=font, fill=(248, 248, 248, 255))
    return w, h


def main():
    img = gradient_bg()
    faint_emblem(img)
    draw = ImageDraw.Draw(img)

    f_name = ImageFont.truetype(FONT_BOLD, NAME_PX)
    f_h1 = ImageFont.truetype(FONT_BOLD, 70)
    f_sub = ImageFont.truetype(FONT_REG, 26)
    f_pill = ImageFont.truetype(FONT_BOLD, 23)
    f_url = ImageFont.truetype(FONT_REG, 24)

    # --- логотип + название (вертикально по центру друг друга) ---
    logo_y = 58
    draw_logo(img, MARGIN, logo_y, LOGO)
    tx = MARGIN + LOGO + 34
    # две строки названия, центрированы относительно лого
    line_gap = 6
    asc, desc = f_name.getmetrics()
    line_h = asc + desc
    block_h = line_h * 2 + line_gap
    ny = logo_y + (LOGO - block_h) // 2
    draw.text((tx, ny), "CONSOLIDATOR", font=f_name, fill=(245, 245, 245, 255))
    draw.text((tx, ny + line_h + line_gap), "PARTS", font=f_name, fill=(245, 245, 245, 255))

    # --- заголовок ---
    draw.text((MARGIN, 248), "Запчасти для спецтехники", font=f_h1, fill=(247, 247, 247, 255))

    # --- подзаголовок ---
    draw.text((MARGIN + 2, 338),
              "Поиск, сравнение предложений и контроль поставки в одном окне",
              font=f_sub, fill=(176, 176, 178, 255))

    # --- плашки ---
    px = MARGIN
    py = 412
    for label in ("Поиск по спецификации", "Сравнение предложений", "Контроль поставки"):
        w, h = rounded_pill(draw, px, py, label, f_pill)
        px += w + 16

    # --- url в правом нижнем углу ---
    url = "consolidatorparts.com"
    ub = draw.textbbox((0, 0), url, font=f_url)
    draw.text((W - MARGIN - (ub[2] - ub[0]), H - 56), url, font=f_url,
              fill=(150, 150, 152, 255))

    img.convert("RGB").save(OUT, "PNG")
    print("saved", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
