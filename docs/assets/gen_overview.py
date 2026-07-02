"""
kioku-mesh overview diagram (static PNG) — light theme, English
"""

import math
import os

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
OUT = os.path.join(os.path.dirname(__file__), 'overview.png')
W, H = 800, 460


def font(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_PATH, size)


# ── light palette ─────────────────────────────────────────────────────────────
BG = (245, 248, 255)
C_MESH_LINE = (180, 205, 240)
C_AGENT_BG = (255, 255, 255)
C_AGENT_RIM = (70, 130, 230)
C_AGENT_TXT = (40, 80, 180)
C_GLOW = (120, 170, 240)
C_BADGE_BG = (235, 242, 255)
C_BADGE_RIM = (100, 155, 235)
C_BADGE_TXT = (30, 65, 160)
C_SUB = (100, 125, 175)
C_TITLE = (25, 50, 130)
C_NO_BG = (255, 238, 238)
C_NO_RIM = (210, 80, 80)
C_NO_TXT = (185, 40, 40)
C_FLOW = (50, 140, 230)


def lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def alpha_blend(base, color, alpha):
    return tuple(int(base[i] * (1 - alpha) + color[i] * alpha) for i in range(3))


img = Image.new('RGB', (W, H), BG)
draw = ImageDraw.Draw(img)

# ── title ─────────────────────────────────────────────────────────────────────
draw.text((W // 2, 30), 'kioku-mesh', fill=C_TITLE, anchor='mm', font=font(22, bold=True))
draw.text(
    (W // 2, 54),
    'A shared memory mesh for your AI coding agents',
    fill=C_SUB,
    anchor='mm',
    font=font(13),
)

# ── nodes ─────────────────────────────────────────────────────────────────────
NODES = {
    'A': (275, 158, 'Home PC'),
    'B': (530, 140, 'Work PC'),
    'C': (400, 248, 'Laptop'),
    'D': (268, 338, 'Raspi'),
    'E': (535, 328, 'Cloud VM'),
}
# cross-tool agents (Claude Code / Codex CLI / Gemini CLI), not a Claude-only mesh.
AGENTS = {
    'A': ('Claude', 'Code'),
    'B': ('Codex', 'CLI'),
    'C': ('Claude', 'Code'),
    'D': ('Gemini', 'CLI'),
    'E': ('Codex', 'CLI'),
}
KEYS = list(NODES.keys())
EDGES = [(KEYS[i], KEYS[j]) for i in range(len(KEYS)) for j in range(i + 1, len(KEYS))]
R_NODE, R_INNER = 38, 27

# mesh lines
for a, b in EDGES:
    x1, y1, _ = NODES[a]
    x2, y2, _ = NODES[b]
    for w, alpha in [(8, 0.10), (3, 0.30), (1, 0.55)]:
        c = alpha_blend(BG, C_MESH_LINE, alpha)
        draw.line([(x1, y1), (x2, y2)], fill=c, width=w)

# flow arrows
FLOW_EDGES = [('A', 'C'), ('C', 'B'), ('C', 'E'), ('D', 'C')]


def draw_arrow(draw, p1, p2, color, width=2):
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    sx, sy = p1[0] + ux * R_NODE, p1[1] + uy * R_NODE
    ex, ey = p2[0] - ux * (R_NODE + 6), p2[1] - uy * (R_NODE + 6)
    draw.line([(sx, sy), (ex, ey)], fill=color, width=width)
    ax, ay = -uy * 6, ux * 6
    draw.polygon(
        [(ex + ux * 9, ey + uy * 9), (ex + ax, ey + ay), (ex - ax, ey - ay)],
        fill=color,
    )


for a, b in FLOW_EDGES:
    x1, y1, _ = NODES[a]
    x2, y2, _ = NODES[b]
    draw_arrow(draw, (x1, y1), (x2, y2), C_FLOW, width=2)

# node circles
for key, (x, y, label) in NODES.items():
    # soft shadow
    for gr in range(R_NODE + 18, R_NODE, -2):
        alpha = 0.025 * (R_NODE + 18 - gr) / 18
        c = alpha_blend(BG, C_GLOW, alpha)
        draw.ellipse((x - gr, y - gr, x + gr, y + gr), fill=c)
    draw.ellipse(
        (x - R_NODE, y - R_NODE, x + R_NODE, y + R_NODE),
        fill=C_AGENT_BG,
        outline=C_AGENT_RIM,
        width=2,
    )
    draw.ellipse(
        (x - R_INNER, y - R_INNER, x + R_INNER, y + R_INNER),
        fill=lerp_color(C_AGENT_BG, C_AGENT_RIM, 0.12),
    )
    agent_line1, agent_line2 = AGENTS[key]
    draw.text((x, y - 6), agent_line1, fill=C_AGENT_TXT, anchor='mm', font=font(11, bold=True))
    draw.text((x, y + 8), agent_line2, fill=lerp_color(C_AGENT_TXT, BG, 0.35), anchor='mm', font=font(10))
    draw.text((x, y + R_NODE + 9), label, fill=C_SUB, anchor='mt', font=font(11))

# ── "No X" badge (top right) ─────────────────────────────────────────────────
bx, by, bw, bh = 620, 75, 158, 50
draw.rounded_rectangle(
    (bx, by, bx + bw, by + bh),
    radius=10,
    fill=C_NO_BG,
    outline=C_NO_RIM,
    width=2,
)
draw.text((bx + bw // 2, by + 14), '✕  No fixed server', fill=C_NO_TXT, anchor='mm', font=font(12))
draw.text((bx + bw // 2, by + 34), '✕  No fixed endpoint', fill=C_NO_TXT, anchor='mm', font=font(12))

# ── callout badges ────────────────────────────────────────────────────────────
BADGES = [
    (15, 182, '✓  Auto mesh discovery', 'Zenoh finds peers automatically'),
    (15, 244, '✓  Works behind NAT', 'No port forwarding or fixed IP'),
    (15, 306, '✓  Fault tolerant', 'Memory survives node failures'),
    (618, 153, '✓  Write anywhere', 'Propagates to all agents'),
    (618, 215, '✓  Zero-config join', 'One command to join the mesh'),
    (618, 277, '✓  Cross-session memory', 'Shared across workers & machines'),
]

for bx2, by2, title, sub in BADGES:
    bw2, bh2 = 165, 48
    draw.rounded_rectangle(
        (bx2, by2, bx2 + bw2, by2 + bh2),
        radius=8,
        fill=C_BADGE_BG,
        outline=C_BADGE_RIM,
        width=1,
    )
    draw.text((bx2 + 10, by2 + 13), title, fill=C_BADGE_TXT, anchor='lm', font=font(11, bold=True))
    draw.text((bx2 + 10, by2 + 33), sub, fill=C_SUB, anchor='lm', font=font(10))

# ── tagline ───────────────────────────────────────────────────────────────────
draw.text(
    (W // 2, H - 16),
    'Multiple machines, multiple agents — one shared memory. No server required.',
    fill=lerp_color(C_SUB, BG, 0.25),
    anchor='mm',
    font=font(12),
)

img.save(OUT)
print(f'saved → {OUT}')
