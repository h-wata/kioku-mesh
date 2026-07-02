"""
kioku-mesh topology animation generator
Produces three animated GIFs:
  1. propagation.gif  — memory write spreading through mesh
  2. recovery.gif     — offline node rejoining and syncing
  3. spoke_join.gif   — new spoke node joining the mesh
"""

import os

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def font(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_PATH, size)


# ─── canvas ─────────────────────────────────────────────────────────────────
W, H = 520, 320
BG = (16, 20, 36)
C_EDGE = (80, 105, 170)
C_NODE = (85, 120, 200)
C_HUB = (45, 145, 235)
C_ACTIVE = (110, 220, 255)
C_PULSE = (170, 235, 255)
C_OFFLINE = (80, 85, 105)
C_RECOVER = (75, 205, 125)
C_NEW = (230, 180, 65)
C_TEXT = (230, 238, 255)
C_LABEL = (185, 208, 240)

R_HUB = 22
R_LEAF = 16
TITLE_SIZE = 14
LABEL_SIZE = 13

OUT_DIR = os.path.dirname(__file__)


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_color(c1, c2, t):
    return tuple(int(lerp(a, b, t)) for a, b in zip(c1, c2))


def alpha_blend(base, color, alpha):
    return tuple(int(b * (1 - alpha) + c * alpha) for b, c in zip(base, color))


def draw_node(draw, x, y, r, fill, outline=None, label='', label_color=C_LABEL):
    glow_r = int(r * 2.2)
    glow_color = alpha_blend(BG, fill, 0.18)
    draw.ellipse((x - glow_r, y - glow_r, x + glow_r, y + glow_r), fill=glow_color)
    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill, outline=outline or fill)
    if label:
        draw.text((x, y + r + 6), label, fill=label_color, anchor='mt', font=font(LABEL_SIZE, bold=True))


def draw_edge(draw, p1, p2, color=C_EDGE, width=2):
    draw.line([p1, p2], fill=color, width=width)


def draw_packet(draw, p1, p2, t, color=C_ACTIVE, r=5):
    x = int(lerp(p1[0], p2[0], t))
    y = int(lerp(p1[1], p2[1], t))
    draw.ellipse((x - r, y - r, x + r, y + r), fill=color)


def draw_title(draw, text):
    draw.text((W // 2, 14), text, fill=C_TEXT, anchor='mm', font=font(TITLE_SIZE, bold=True))


def new_frame():
    img = Image.new('RGB', (W, H), BG)
    return img, ImageDraw.Draw(img)


def save_gif(frames, path, duration=60, loop=0):
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        optimize=False,
        duration=duration,
        loop=loop,
    )
    print(f'  saved → {path}')


# ─── topology ────────────────────────────────────────────────────────────────
#
#  Hub A (left)  <──────>  Hub B (right)
#   │   │                    │   │
#  S1  S2                   S3  S4
#
HUB_A = (155, 160)
HUB_B = (365, 160)
S1 = (75, 255)
S2 = (175, 255)
S3 = (325, 255)
S4 = (435, 255)

HUBS = [HUB_A, HUB_B]
SPOKES = [S1, S2, S3, S4]
NODES = HUBS + SPOKES

EDGES = [
    (HUB_A, HUB_B),
    (HUB_A, S1),
    (HUB_A, S2),
    (HUB_B, S3),
    (HUB_B, S4),
]

NODE_LABELS = {
    HUB_A: 'Hub-A',
    HUB_B: 'Hub-B',
    S1: 'S1',
    S2: 'S2',
    S3: 'S3',
    S4: 'S4',
}


def base_frame(highlight=None, offline=None, new_node=None, new_edges=None, pulse_nodes=None, pulse_t=0.0, title=''):
    img, draw = new_frame()
    draw_title(draw, title)

    # edges
    for a, b in EDGES:
        draw_edge(draw, a, b)
    if new_edges:
        for a, b in new_edges:
            draw_edge(draw, a, b, color=lerp_color(BG, C_NEW, 0.6), width=2)

    # nodes
    for n in NODES:
        if offline and n in offline:
            fill = C_OFFLINE
            r = R_LEAF
        elif n in HUBS:
            r = R_HUB
            fill = C_HUB if not (highlight and n in highlight) else C_ACTIVE
        else:
            r = R_LEAF
            fill = C_NODE if not (highlight and n in highlight) else C_ACTIVE
        draw_node(draw, n[0], n[1], r, fill, label=NODE_LABELS[n])

    if pulse_nodes:
        for n in pulse_nodes:
            base_r = R_HUB if n in HUBS else R_LEAF
            # stay within a tight halo around the node rim so the ring never
            # sweeps over the label drawn just below (label starts at r + 6).
            pr = base_r + int(pulse_t * 5)
            alpha = max(0.0, 0.65 - pulse_t * 0.65)
            ring_color = alpha_blend(BG, C_PULSE, alpha)
            draw.ellipse((n[0] - pr, n[1] - pr, n[0] + pr, n[1] + pr), outline=ring_color, width=3)

    if new_node:
        draw_node(draw, new_node[0], new_node[1], R_LEAF, C_NEW, label=new_node[2] if len(new_node) > 2 else 'New')

    return img, draw


# ─── 1. propagation ──────────────────────────────────────────────────────────
def make_propagation():
    frames = []
    TOTAL_FRAMES = 80
    # sequence:
    #  0-9:   idle
    #  10-19: S2 writes  (S2 bright)
    #  20-34: packet S2→HubA
    #  35-49: packet HubA→HubB  + HubA lit
    #  50-64: packet HubB→S3, HubB→S4
    #  65-79: all lit, fade out

    for f in range(TOTAL_FRAMES):
        highlight = set()
        pkts = []

        if f >= 10:
            highlight.add(S2)
        if 20 <= f < 35:
            pkts.append((S2, HUB_A, (f - 20) / 15))
        if f >= 35:
            highlight.add(HUB_A)
        if 35 <= f < 50:
            pkts.append((HUB_A, HUB_B, (f - 35) / 15))
        if f >= 50:
            highlight.add(HUB_B)
        if 50 <= f < 65:
            pkts.append((HUB_B, S3, (f - 50) / 15))
            pkts.append((HUB_B, S4, (f - 50) / 15))
        if f >= 65:
            highlight.update([S3, S4])

        pulse_t = ((f % 20) / 20) if f >= 10 else 0
        pulse_nodes = list(highlight) if f >= 10 else []

        img, draw = base_frame(
            highlight=highlight,
            pulse_nodes=pulse_nodes,
            pulse_t=pulse_t,
            title='Memory Propagation — write on S2 spreads to mesh',
        )
        for src, dst, pt in pkts:
            draw_packet(draw, src, dst, pt)
        frames.append(img)

    path = os.path.join(OUT_DIR, 'propagation.gif')
    save_gif(frames, path, duration=55)


# ─── 2. recovery ─────────────────────────────────────────────────────────────
def make_recovery():
    frames = []
    TOTAL = 90
    # 0-14:  S3 is offline (gray)
    # 15-25: S3 reconnects (glow)
    # 26-50: catch-up packets HubB→S3
    # 51-70: S3 fully lit, pulse
    # 71-89: settle

    for f in range(TOTAL):
        offline = set()
        highlight = set()
        pkts = []
        pulse_nodes = []
        pulse_t = 0.0

        if f < 25:
            offline.add(S3)
            if f >= 15:
                # fade-in effect: show node half-bright
                pass  # handled after base_frame

        if f >= 25:
            highlight.add(HUB_B)
        if 26 <= f < 50:
            pkts.append((HUB_B, S3, (f - 26) / 24))
        if f >= 50:
            highlight.add(S3)
            pulse_nodes = [S3]
            pulse_t = ((f - 50) % 20) / 20

        img, draw = base_frame(
            highlight=highlight,
            offline=offline if f < 25 else None,
            pulse_nodes=pulse_nodes,
            pulse_t=pulse_t,
            title='Node Recovery — S3 reconnects and syncs missed data',
        )

        # fade-in during 15-24
        if 15 <= f < 25:
            alpha = (f - 15) / 10
            c = lerp_color(C_OFFLINE, C_RECOVER, alpha)
            draw.ellipse(
                (S3[0] - R_LEAF, S3[1] - R_LEAF, S3[0] + R_LEAF, S3[1] + R_LEAF),
                fill=c,
            )

        for src, dst, pt in pkts:
            draw_packet(draw, src, dst, pt, color=C_RECOVER)
        frames.append(img)

    path = os.path.join(OUT_DIR, 'recovery.gif')
    save_gif(frames, path, duration=60)


# ─── 3. spoke join ───────────────────────────────────────────────────────────
def make_spoke_join():
    frames = []
    TOTAL = 90
    # new spoke S5 appears near HubB
    S5 = (445, 75)
    S5_LABEL = 'S5'

    for f in range(TOTAL):
        highlight = set()
        new_edges_alpha = 0.0
        pulse_t = 0.0
        pulse_nodes = []
        show_new = False

        if f < 20:
            pass  # stable mesh, no new node
        elif f < 35:
            show_new = True  # new node appears, no edge yet
        elif f < 55:
            show_new = True
            new_edges_alpha = (f - 35) / 20  # edge fades in
        else:
            show_new = True
            new_edges_alpha = 1.0
            highlight.update([HUB_B, (S5[0], S5[1])])
            pulse_nodes = [HUB_B]
            pulse_t = ((f - 55) % 20) / 20

        img, draw = base_frame(
            highlight=highlight,
            pulse_nodes=pulse_nodes,
            pulse_t=pulse_t,
            title='Spoke Join — new node S5 joins the mesh',
        )

        if show_new:
            edge_c = lerp_color(BG, C_NEW, new_edges_alpha)
            draw_edge(draw, HUB_B, (S5[0], S5[1]), color=edge_c, width=2)
            n_fill = lerp_color(BG, C_NEW, min(1.0, (f - 20) / 10))
            draw_node(
                draw,
                S5[0],
                S5[1],
                R_LEAF,
                n_fill,
                label=S5_LABEL,
                label_color=lerp_color(BG, C_LABEL, min(1.0, (f - 20) / 10)),
            )

            if f >= 55:
                # discovery packet
                pt = ((f - 55) % 25) / 25
                draw_packet(draw, (S5[0], S5[1]), HUB_B, pt, color=C_NEW)

        frames.append(img)

    path = os.path.join(OUT_DIR, 'spoke_join.gif')
    save_gif(frames, path, duration=60)


# ─── run ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('Generating kioku-mesh animations...')
    make_propagation()
    make_recovery()
    make_spoke_join()
    print('Done.')
