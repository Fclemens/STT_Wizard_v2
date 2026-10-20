"""Regenerate assets/icon.ico (the waveform mark from the page header). Needs Pillow."""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((8, 8, SIZE - 8, SIZE - 8), radius=56, fill=(14, 124, 102, 255))
# Five bars, as in the header SVG (16x16 viewBox): x, y, height
bars = [(0.5, 5, 6), (4, 2.5, 11), (7.5, 0.5, 15), (11, 3.5, 9), (14.5, 6, 5)]
scale, offset = 9.5, 50
for x, y, h in bars:
    left, top = offset + x * scale, offset + y * scale + 4
    draw.rounded_rectangle((left, top, left + 2 * scale, top + h * scale), radius=scale, fill="white")
target = Path(__file__).with_name("icon.ico")
image.save(target, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"wrote {target}")
