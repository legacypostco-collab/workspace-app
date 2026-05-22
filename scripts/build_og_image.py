"""Generate static/brand/og-image.png (1200x630) for Open Graph / Twitter."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "static" / "brand" / "og-image.png"
W, H = 1200, 630

img = Image.new("RGB", (W, H), (16, 16, 20))
draw = ImageDraw.Draw(img)
# нижняя оранжевая полоса
draw.rectangle([0, H - 8, W, H], fill=(255, 102, 0))

def _font(size: int):
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()

font_big = _font(88)
font_mid = _font(40)
font_sm  = _font(34)

draw.text((80, 200), "Consolidator Parts", fill=(255, 255, 255), font=font_big)
draw.text((84, 330), "B2B-маркетплейс запчастей для спецтехники",
           fill=(200, 200, 200), font=font_mid)
draw.text((84, 410), "200+ поставщиков · ETA 2.3 ч · −18% к рынку",
           fill=(255, 102, 0), font=font_sm)

OUT.parent.mkdir(parents=True, exist_ok=True)
img.save(OUT, "PNG", optimize=True)
print(f"OK: {OUT} ({OUT.stat().st_size} bytes)")
