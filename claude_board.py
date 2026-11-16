"""Claude Code sessions on the Lightning Z screen.

The user's `claude-board` agent (fed by Claude Code hooks, running in the user's own session) pushes
the session list over the control socket, so the service never reads anything in /home:

  {"mode": "claude-status", "sessions": [{"id": "...", "title": "...", "project": "...",
     "pos": "1.3", "where": "tmux", "state": "needs", "since": 1763300000.0, "msg": "..."}]}

state: "needs"   waiting for an answer or a permission
       "done"    finished and not looked at yet
       "working" busy
       "idle"    finished and seen

The GPU dashboard turns into this view by itself while any session is "needs" or "done" (the GPU
numbers shrink to a strip in the header); `lightning-screen claude` keeps it on screen.
"""
import functools, math, os, threading, time
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
DEJAVU = "/usr/share/fonts/truetype/dejavu/"
FPS = 12                     # frame rate while this view is on screen (animations)
STALE = 30                   # seconds without a push from the agent before the list is dropped

# ---- Claude palette (dark, warm) ---------------------------------------------------------------
BG = (31, 30, 29)
CARD = (40, 39, 36)
CARD_HI = (50, 48, 44)
CHIP = (61, 59, 54)
INK = (250, 249, 245)
INK2 = (205, 202, 192)
MUTED = (148, 144, 133)
CLAUDE = (217, 119, 87)      # Claude orange
DONE = (126, 186, 135)
SS = 3                       # supersampling for anti-aliased shapes


# ---- session list pushed by the agent ----------------------------------------------------------
class Board:
    STATES = ("needs", "done", "working", "idle")

    def __init__(self):
        self.lock, self.sessions, self.at = threading.Lock(), [], 0.0

    def update(self, sessions):
        clean = []
        for s in list(sessions)[:60]:
            if not isinstance(s, dict) or s.get("state") not in self.STATES:
                continue
            clean.append({"id": str(s.get("id", ""))[:64], "title": str(s.get("title") or "")[:200],
                          "project": str(s.get("project") or "")[:80], "pos": str(s.get("pos") or "")[:24],
                          "where": str(s.get("where") or "")[:16], "state": s["state"],
                          "msg": str(s.get("msg") or "")[:160], "since": _num(s.get("since"))})
        with self.lock:
            self.sessions, self.at = clean, time.monotonic()

    def snapshot(self):
        with self.lock:
            return [] if time.monotonic() - self.at > STALE else list(self.sessions)

    def attention(self):
        return any(s["state"] in ("needs", "done") for s in self.snapshot())

    def active(self):
        """Something to show: a session is working, needs an answer or finished unseen."""
        return any(s["state"] in ("working", "needs", "done") for s in self.snapshot())


def _num(v):
    try: return float(v)
    except (TypeError, ValueError): return time.time()


# ---- fonts --------------------------------------------------------------------------------------
@functools.lru_cache(maxsize=96)
def font(kind, size, weight=400):
    """kind: "sans" (Inter), "serif" (Source Serif 4) or "sym" (DejaVu, for the spinner glyphs)."""
    if kind == "sym":
        return ImageFont.truetype(DEJAVU + "DejaVuSans.ttf", size)
    name = "Inter.ttf" if kind == "sans" else "SourceSerif4.ttf"
    try:
        f = ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        return ImageFont.truetype(DEJAVU + ("DejaVuSans.ttf" if kind == "sans" else "DejaVuSerif.ttf"), size)
    try:
        vals = []
        for ax in f.get_variation_axes():
            n = ax["name"].decode() if isinstance(ax["name"], bytes) else str(ax["name"])
            if n.lower().startswith("weight"):
                vals.append(weight)
            elif n.lower().startswith("optical"):
                vals.append(max(ax["minimum"], min(ax["maximum"], size)))
            else:
                vals.append(ax["default"])
        f.set_variation_by_axes(vals)
    except Exception:
        pass
    return f


def fit(d, text, f, maxw):
    """text shortened with an ellipsis to fit maxw pixels."""
    if d.textlength(text, font=f) <= maxw:
        return text
    while text and d.textlength(text + "…", font=f) > maxw:
        text = text[:-1]
    return text.rstrip() + "…"


def spaced(d, xy, text, f, fill, track=1.5, anchor_right=False):
    """Letter-spaced small caps label; returns its width."""
    w = sum(d.textlength(ch, font=f) for ch in text) + track * (len(text) - 1)
    x, y = xy
    if anchor_right:
        x -= w
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += d.textlength(ch, font=f) + track
    return w


# ---- anti-aliased shapes (cached sprites) ------------------------------------------------------
@functools.lru_cache(maxsize=256)
def rrect(w, h, r, fill, outline=None, width=0):
    w, h = max(1, int(w)), max(1, int(h))
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], r * SS, fill=fill,
                                          outline=outline, width=width * SS)
    return big.resize((w, h), Image.LANCZOS)


@functools.lru_cache(maxsize=64)
def glow(w, h, r, color, spread, level):
    pad = spread * 2
    img = Image.new("RGBA", (int(w) + 2 * pad, int(h) + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([pad, pad, pad + w, pad + h], r, fill=color + (min(255, 26 * level),))
    return img.filter(ImageFilter.GaussianBlur(spread))


@functools.lru_cache(maxsize=32)
def check_icon(size, color, ring):
    """A round check mark; ring (0-8) is the pulse of an outer halo."""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, r = S / 2, S * 0.36
    if ring:
        rr = r + (S * 0.49 - r) * ring / 8
        d.ellipse([c - rr, c - rr, c + rr, c + rr], outline=color + (max(0, 150 - 18 * ring),), width=int(S * 0.03))
    d.ellipse([c - r, c - r, c + r, c + r], fill=color + (255,))
    w = int(S * 0.075)
    pts = [(c - r * 0.45, c + r * 0.02), (c - r * 0.1, c + r * 0.36), (c + r * 0.5, c - r * 0.32)]
    d.line(pts, fill=BG + (255,), width=w, joint="curve")
    for p in (pts[0], pts[2]):
        d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=BG + (255,))
    return img.resize((size, size), Image.LANCZOS)


# Claude's spark: tapered, rounded rays of uneven length around a small core
RAYS = ((0, 1.00), (31, .80), (59, .94), (91, .73), (120, .98), (151, .83),
        (180, .96), (209, .76), (240, .99), (269, .81), (300, .92), (330, .77))


def spark(size, t, color=CLAUDE, breathe=0.0, spin=0.12):
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, R, w0 = S / 2, S * 0.48, S * 0.095
    rot = t * spin
    for i, (deg, ln) in enumerate(RAYS):
        a = math.radians(deg) + rot
        L = min(R, R * ln * (1 + breathe * math.sin(t * 2.6 + i * 1.7)))
        ca, sa = math.cos(a), math.sin(a)
        px, py = -sa, ca
        x0, y0, x1, y1 = c + ca * S * 0.05, c + sa * S * 0.05, c + ca * (L - w0 * 0.3), c + sa * (L - w0 * 0.3)
        w1 = w0 * 0.62
        d.polygon([(x0 + px * w0 / 2, y0 + py * w0 / 2), (x1 + px * w1 / 2, y1 + py * w1 / 2),
                   (x1 - px * w1 / 2, y1 - py * w1 / 2), (x0 - px * w0 / 2, y0 - py * w0 / 2)], fill=color)
        d.ellipse([x1 - w1 / 2, y1 - w1 / 2, x1 + w1 / 2, y1 + w1 / 2], fill=color)
    d.ellipse([c - w0, c - w0, c + w0, c + w0], fill=color)
    return img.resize((size, size), Image.LANCZOS)


# The Claude Code mascot, from the block characters of the CLI's start-up banner (2x2 px per char)
CLAWD = (" ▐▛███▜▌", "▝▜█████▛▘", "  ▘▘ ▝▝")
QUAD = {" ": "0000", "█": "1111", "▌": "1010", "▐": "0101", "▀": "1100", "▄": "0011", "▘": "1000",
        "▝": "0100", "▖": "0010", "▗": "0001", "▛": "1110", "▜": "1101", "▙": "1011", "▟": "0111",
        "▚": "1001", "▞": "0110"}


def _grid(rows):
    w = max(map(len, rows)) * 2
    g = [[0] * w for _ in range(len(rows) * 2)]
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            for k, (dy, dx) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
                if QUAD.get(ch, "0000")[k] == "1":
                    g[r * 2 + dy][c * 2 + dx] = 1
    return g


GRID = _grid(CLAWD)
_ROW1 = [x for x, v in enumerate(GRID[1]) if v]
EYES = [(1, x) for x in range(_ROW1[0], _ROW1[-1]) if not GRID[1][x]]     # holes in the face
ARM_R = max(x for x, v in enumerate(GRID[2]) if v)                        # right hand pixel


def mascot(scale, t, mood="idle", color=CLAUDE):
    """Pixel mascot. mood: "wave" (needs you), "hop" (finished), "idle", "sleep"."""
    g = [row[:] for row in GRID]
    blink = (t % 3.4) < 0.14 or mood == "sleep"
    if blink:
        for y, x in EYES:
            g[y][x] = 1
    if mood == "wave" and int(t * 4) % 2:
        g[2][ARM_R] = 0
        g[1][ARM_R] = g[0][ARM_R] = 1
    h, w = len(g), len(g[0])
    small = Image.new("RGBA", (w, h + 2), (0, 0, 0, 0))
    lift = 0
    if mood == "hop":
        lift = 2 if (t % 0.9) < 0.3 else (1 if (t % 0.9) < 0.42 else 0)
    elif mood in ("wave", "idle"):
        lift = 1 if int(t * (3 if mood == "wave" else 1.2)) % 2 else 0
    for y in range(h):
        for x in range(w):
            if g[y][x]:
                small.putpixel((x, y + 2 - lift), color + (255,))
    # terminal cells are about twice as tall as wide: each banner pixel is 1 x 2
    return small.resize((w * scale, (h + 2) * scale * 2), Image.NEAREST)


SPIN = "·✢✳✶✻✽"
SPIN_FRAMES = SPIN + SPIN[-2:0:-1]


def spinner(t):
    return SPIN_FRAMES[int(t * 7) % len(SPIN_FRAMES)]


def ago(since, now):
    s = max(0, int(now - since))
    if s < 60: return "just now" if s < 10 else f"{s} s"
    if s < 3600: return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def place(s):
    if s["where"] == "tmux" and s["pos"]:
        return "tmux " + s["pos"]
    return {"app": "Claude app", "vscode": "VS Code", "terminal": "terminal"}.get(s["where"], s["where"] or "—")


ORDER = {"needs": 0, "done": 1, "working": 2, "idle": 3}


def ordered(sessions):
    return sorted(sessions, key=lambda s: (ORDER[s["state"]], s["since"] if s["state"] != "done" else -s["since"]))


# ---- the full Claude view ---------------------------------------------------------------------
def render(W, H, sessions, g, c, t=None):
    """sessions as pushed by the agent; g, c: GPU and CPU readings of the dashboard (may be None)."""
    t = time.time() if t is None else t
    img = Image.new("RGBA", (W, H), BG + (255,))
    d = ImageDraw.Draw(img)
    pad = 28
    ss = ordered(sessions)
    att = [s for s in ss if s["state"] in ("needs", "done")]
    rest = [s for s in ss if s["state"] not in ("needs", "done")]
    busy = any(s["state"] == "working" for s in ss)

    # header: spark + wordmark, GPU strip, clock
    img.alpha_composite(spark(50, t, breathe=0.07 if busy else 0.0, spin=0.35 if busy else 0.08), (pad - 4, 18))
    d.text((pad + 56, 20), "Claude", font=font("serif", 38, 500), fill=INK)
    sub = f"{len(ss)} session{'s' if len(ss) != 1 else ''}" if ss else "no sessions"
    d.text((pad + 56 + d.textlength("Claude", font=font("serif", 38, 500)) + 14, 36), sub,
           font=font("sans", 19, 400), fill=MUTED)
    clock = time.strftime("%H:%M")
    fc = font("sans", 28, 500)
    d.text((W - pad, 26), clock, font=fc, fill=INK2, anchor="ra")
    x = W - pad - d.textlength(clock, font=fc) - 22
    for label, val in reversed(_gpu_strip(g, c)):
        fl, fv = font("sans", 16, 500), font("sans", 18, 600)
        wl, wv = d.textlength(label, font=fl), d.textlength(val, font=fv)
        pw = int(wl + wv + 34)
        x -= pw
        img.alpha_composite(rrect(pw, 36, 18, CARD), (int(x), 24))
        d.text((x + 14, 42), label, font=fl, fill=MUTED, anchor="lm")
        d.text((x + 20 + wl, 42), val, font=fv, fill=INK, anchor="lm")
        x -= 10

    y0, bottom = 100, H - 22
    if not ss:
        _empty(img, d, W, H, t)
        return img

    # hero when nothing needs you
    if not att:
        hero_h = 150
        img.alpha_composite(mascot(6, t, "idle"), (pad + 30, y0 + 22))
        d.text((pad + 220, y0 + 30), "All caught up", font=font("serif", 40, 500), fill=INK)
        n = sum(s["state"] == "working" for s in ss)
        d.text((pad + 220, y0 + 86), f"{n} working · nothing needs you" if n else "Nothing needs you right now",
               font=font("sans", 21, 400), fill=MUTED)
        y0 += hero_h

    # attention cards
    gap = 14
    row_h = 62 if len(ss) <= 6 else 54
    card_h = 168 if len(att) <= 2 else (130 if len(att) <= 3 else 104)
    rows_wanted = min(len(rest), 3)
    space = bottom - y0 - ((34 + rows_wanted * (row_h + 8)) if rows_wanted else 0)
    n_cards = min(len(att), max(1, (space + gap) // (card_h + gap))) if att else 0
    y = y0
    for s in att[:n_cards]:
        _card(img, d, pad, y, W - 2 * pad, card_h, s, t)
        y += card_h + gap
    hidden = len(att) - n_cards

    # other sessions
    if rest:
        if att:
            y += 6
            spaced(d, (pad + 4, y), "IN PROGRESS" if any(s["state"] == "working" for s in rest) else "OTHER SESSIONS",
                   font("sans", 15, 700), MUTED, track=2.2)
            y += 28
        n_rows = max(0, min(len(rest), (bottom - y + 8) // (row_h + 8)))
        if n_rows < len(rest) and n_rows:
            n_rows -= 1
        for s in rest[:n_rows]:
            _row(img, d, pad, y, W - 2 * pad, row_h, s, t)
            y += row_h + 8
        hidden += len(rest) - n_rows
    if hidden > 0:
        d.text((W // 2, min(y + 4, bottom - 24)), f"+ {hidden} more", font=font("sans", 18, 500), fill=MUTED, anchor="ma")
    return img


def _gpu_strip(g, c):
    out = []
    if g:
        if g.get("temp") is not None: out.append(("GPU", f"{g['temp']:.0f}°C"))
        if g.get("power") is not None: out.append(("", f"{g['power']:.0f} W"))
        if g.get("load") is not None: out.append(("load", f"{g['load']:.0f}%"))
    if c and c.get("temp") is not None:
        out.append(("CPU", f"{c['temp']:.0f}°C"))
    return out


def _empty(img, d, W, H, t):
    m = mascot(11, t, "sleep")
    img.alpha_composite(m, ((W - m.width) // 2, H // 2 - 170))
    for i in range(3):                                      # drifting z's
        p = (t * 0.45 + i / 3) % 1.0
        f = font("serif", int(22 + 18 * p), 500)
        a = int(255 * (1 - p) * min(1, p * 4))
        zx, zy = W // 2 + m.width // 2 - 10 + int(40 * p), H // 2 - 170 - int(90 * p)
        d.text((zx, zy), "z", font=f, fill=MUTED + (a,))
    d.text((W // 2, H // 2 + 20), "No Claude sessions", font=font("serif", 40, 500), fill=INK, anchor="ma")
    d.text((W // 2, H // 2 + 78), "Start one in tmux and it will show up here", font=font("sans", 21, 400),
           fill=MUTED, anchor="ma")


def _card(img, d, x, y, w, h, s, t):
    needs = s["state"] == "needs"
    col = CLAUDE if needs else DONE
    now = time.time()
    if needs:                                              # breathing glow
        level = 3 + round(4 * (0.5 + 0.5 * math.sin(t * 2 * math.pi / 1.8)))
        gl = glow(w, h, 20, CLAUDE, 14, level)
        img.alpha_composite(gl, (x - 28, y - 28))
    img.alpha_composite(rrect(w, h, 20, CARD_HI if needs else CARD), (x, y))
    img.alpha_composite(rrect(6, h - 36, 3, col), (x + 14, y + 18))

    # icon
    icx = x + 38
    if needs:
        scale = 6 if h >= 150 else (5 if h >= 118 else 4)
        m = mascot(scale, t + hash(s["id"]) % 7 * 0.13, "wave")
        img.alpha_composite(m, (icx, y + (h - m.height) // 2))
        tx = icx + m.width + 30
    else:
        size = 64 if h >= 118 else 54
        ring = int((t * 6) % 9) if (now - s["since"]) < 90 else 0
        img.alpha_composite(check_icon(size + 24, DONE, ring), (icx + 8, y + (h - size - 24) // 2))
        tx = icx + 126

    # right: state label + time
    label = "NEEDS YOUR ANSWER" if needs else "FINISHED"
    spaced(d, (x + w - 28, y + h // 2 - 30), label, font("sans", 15, 700), col, track=2.2, anchor_right=True)
    when = ("waiting " if needs else "") + ago(s["since"], now) + ("" if needs else " ago")
    if when.startswith("just now ago"): when = "just now"
    d.text((x + w - 28, y + h // 2 - 2), when, font=font("sans", 24, 500), fill=INK2, anchor="ra")
    right = x + w - 28 - max(d.textlength(when, font=font("sans", 24, 500)), 190) - 30

    # title + meta
    ft = font("serif", 33 if h >= 118 else 28, 600)
    title = fit(d, s["title"] or s["project"] or "Claude session", ft, right - tx)
    ty = y + h // 2 - (44 if s["msg"] and h >= 118 else 34)
    d.text((tx, ty), title, font=ft, fill=INK)
    my = ty + (50 if h >= 118 else 42)
    mx = tx + _chip(img, d, tx, my, place(s), col) + 14
    fm = font("sans", 19, 400)
    d.text((mx, my + 15), fit(d, s["project"], fm, right - mx), font=fm, fill=MUTED, anchor="lm")
    if s["msg"] and h >= 118:
        d.text((tx, my + 38), fit(d, s["msg"], fm, right - tx), font=fm, fill=INK2)


def _chip(img, d, x, y, text, col):
    f = font("sans", 17, 600)
    w = int(d.textlength(text, font=f) + 24)
    img.alpha_composite(rrect(w, 30, 15, col + (46,)), (int(x), int(y)))
    d.text((x + w / 2, y + 15), text, font=f, fill=col, anchor="mm")
    return w


def _row(img, d, x, y, w, h, s, t):
    working = s["state"] == "working"
    img.alpha_composite(rrect(w, h, 14, CARD), (x, y))
    cy = y + h // 2
    if working:
        d.text((x + 34, cy + 1), spinner(t + (hash(s["id"]) % 10) * 0.07), font=font("sym", 28), fill=CLAUDE, anchor="mm")
    else:
        d.ellipse([x + 29, cy - 5, x + 39, cy + 5], fill=MUTED)
    cw = _chip(img, d, x + 62, cy - 15, place(s), CLAUDE if working else MUTED)
    tx = x + 62 + cw + 16
    fr = font("sans", 17, 500)
    when = ("working " if working else "idle ") + ago(s["since"], time.time())
    d.text((x + w - 22, cy), when, font=fr, fill=MUTED, anchor="rm")
    right = x + w - 22 - d.textlength(when, font=fr) - 24
    ft = font("sans", 22, 600 if working else 500)
    title = fit(d, s["title"] or s["project"] or "Claude session", ft, max(40, right - tx - 160))
    d.text((tx, cy), title, font=ft, fill=INK if working else INK2, anchor="lm")
    px = tx + d.textlength(title, font=ft) + 14
    if s["project"] and px < right - 40:
        d.text((px, cy + 1), fit(d, s["project"], font("sans", 18, 400), right - px), font=font("sans", 18, 400),
               fill=MUTED, anchor="lm")


# ---- small badge on the GPU dashboard ----------------------------------------------------------
def badge(img, right, y, sessions, t):
    """'✻ 2 working' (or 'Claude · 3 idle') next to the dashboard clock; returns nothing."""
    if not sessions:
        return
    d = ImageDraw.Draw(img)
    n = sum(s["state"] == "working" for s in sessions)
    text = f"{n} working" if n else f"{len(sessions)} idle"
    f = font("sans", 20, 600)
    tw = d.textlength(text, font=f)
    w = int(tw + 62)
    x = int(right - w)
    base = img.convert("RGBA")
    base.alpha_composite(rrect(w, 38, 19, CARD_HI), (x, y))
    sp = spark(26, t, breathe=0.08 if n else 0, spin=0.4 if n else 0.05)
    base.alpha_composite(sp, (x + 12, y + 6))
    img.paste(base)
    d = ImageDraw.Draw(img)
    d.text((x + 46, y + 19), text, font=f, fill=INK if n else INK2, anchor="lm")
