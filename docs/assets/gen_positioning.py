"""kioku-mesh positioning map (red-ocean vs open niche), PIL, light theme."""

import os

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
OUT = os.path.join(os.path.dirname(__file__), 'positioning.png')
W, H = 1100, 840


def font(sz, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_PATH, sz)


BG = (245, 248, 255)
img = Image.new('RGB', (W, H), BG)
d = ImageDraw.Draw(img)

# plot area
L, R, T, B = 120, W - 40, 90, H - 90
cx, cy = (L + R) // 2, (T + B) // 2


def X(u):  # u in 0..10
    return L + (R - L) * u / 10


def Y(v):  # v in 0..10 (bottom origin)
    return B - (B - T) * v / 10


# title
d.text((W // 2, 36), 'AI agent memory — positioning map', font=font(30, True), fill=(30, 40, 60), anchor='mm')

# open-niche highlight (top-right quadrant)
d.rectangle([X(5.1), Y(9.8), X(9.8), Y(5.1)], fill=(232, 246, 238), outline=(46, 139, 87), width=3)
d.text((X(9.6), Y(9.6)), 'OPEN NICHE', font=font(20, True), fill=(46, 139, 87), anchor='ra')

# quadrant guides
d.line([cx, T, cx, B], fill=(200, 205, 215), width=1)
d.line([L, cy, R, cy], fill=(200, 205, 215), width=1)

# axes arrows
d.line([L, B, R, B], fill=(70, 70, 80), width=3)
d.line([L, B, L, T], fill=(70, 70, 80), width=3)
d.polygon([(R, B), (R - 14, B - 7), (R - 14, B + 7)], fill=(70, 70, 80))
d.polygon([(L, T), (L - 7, T + 14), (L + 7, T + 14)], fill=(70, 70, 80))

d.text(
    (cx, B + 46),
    'Single machine        ───────▶        Multi-host / fleet',
    font=font(19, True),
    fill=(50, 55, 70),
    anchor='mm',
)
tmp = Image.new('RGBA', (560, 30), (0, 0, 0, 0))
ImageDraw.Draw(tmp).text(
    (280, 15),
    'SaaS / central store   ─────▶   Self-hosted P2P mesh',
    font=font(19, True),
    fill=(50, 55, 70),
    anchor='mm',
)
img.paste(tmp.rotate(90, expand=True), (8, cy - 280), tmp.rotate(90, expand=True))

RED, ORG, BLU, GRN = (217, 83, 79), (224, 142, 11), (91, 141, 239), (46, 139, 87)


def chip(u, v, label, sub, color, star=None, big=False):
    px, py = X(u), Y(v)
    rad = 16 if big else 10
    d.ellipse([px - rad, py - rad, px + rad, py + rad], fill=color, outline=(255, 255, 255), width=3)
    t = label + (f'  *{star}' if star else '')
    d.text((px, py + rad + 6), t, font=font(18 if big else 15, True), fill=(34, 34, 34), anchor='ma')
    if sub:
        d.text((px, py + rad + (32 if big else 28)), sub, font=font(13, False), fill=(110, 110, 110), anchor='ma')


# red-ocean cluster (single machine + central/SaaS)
chip(2.1, 2.6, 'mem0', 'SaaS memory layer', RED, star='35k+')
chip(3.5, 3.6, 'hindsight', 'learns over time', RED, star='17.7k')
chip(1.7, 3.9, 'memU', 'workspace memory', RED, star='13.9k')
chip(3.7, 1.8, 'A-mem', 'agentic memory', ORG, star='1k')
chip(2.6, 1.1, 'memory-mcp / many...', 'single-host MCP', ORG)

# MisakaNet: multi-host-ish but git/offline
chip(6.5, 6.2, 'MisakaNet', 'git PR + CI, offline', BLU)

# kioku-mesh — the niche
chip(8.4, 8.4, 'kioku-mesh', 'Zenoh P2P / realtime / cross-tool', GRN, big=True)

# red-ocean callout
d.rounded_rectangle([X(0.4), Y(5.6), X(4.7), Y(4.6)], radius=8, fill=(253, 234, 234), outline=RED, width=2)
d.text(
    ((X(0.4) + X(4.7)) // 2, (Y(5.6) + Y(4.6)) // 2),
    '"agent memory" red ocean\n(time-axis memory, 1 box)',
    font=font(14, False),
    fill=(150, 40, 40),
    anchor='mm',
    align='center',
)

img.save(OUT)
print('saved', OUT)
