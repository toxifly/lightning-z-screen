"""Claude Code sessions on the Lightning Z screen.

The user's `claude-board` agent (fed by Claude Code hooks, running in the user's own session) pushes
the session list over the control socket, so the service never reads anything in /home:

  {"mode": "claude-status", "sessions": [{"id": "...", "title": "...", "project": "...",
     "pos": "1.3", "where": "tmux", "state": "needs", "since": 1763300000.0, "msg": "..."}]}

state: "needs"   waiting for an answer or a permission
       "done"    finished and not looked at yet
       "working" busy
       "idle"    finished and seen

The GPU dashboard turns into this view by itself while any session is working, needs an answer or
is done (the GPU numbers shrink to a strip in the header); `lightning-screen claude` keeps it on screen.
"""
import functools, math, os, threading, time
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
DEJAVU = "/usr/share/fonts/truetype/dejavu/"
FPS = 2                      # frame rate while a session is working (comet, spinners)
CALM_FPS = 2                 # frame rate otherwise: needs/done/idle tiles barely move
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
        self.lock, self.sessions, self.windows, self.at = threading.Lock(), [], [], 0.0

    def update(self, sessions, tmux=()):
        clean = []
        for s in list(sessions)[:60]:
            if not isinstance(s, dict) or s.get("state") not in self.STATES:
                continue
            clean.append({"id": str(s.get("id", ""))[:64], "title": str(s.get("title") or "")[:200],
                          "project": str(s.get("project") or "")[:80], "pos": str(s.get("pos") or "")[:24],
                          "where": str(s.get("where") or "")[:16], "state": s["state"],
                          "msg": str(s.get("msg") or "")[:160], "since": _num(s.get("since"))})
        windows = []
        for w in list(tmux or ())[:8]:
            try:
                panes = [{"pos": str(p["pos"])[:24], "x": int(p["x"]), "y": int(p["y"]), "w": max(1, int(p["w"])),
                          "h": max(1, int(p["h"])), "cmd": str(p.get("cmd") or "")[:24], "focus": bool(p.get("focus"))}
                         for p in list(w["panes"])[:16]]
                windows.append({"win": str(w["win"])[:24], "name": str(w.get("name") or "")[:40],
                                "active": bool(w.get("active")), "w": max(1, int(w["w"])), "h": max(1, int(w["h"])),
                                "panes": panes})
            except (KeyError, TypeError, ValueError):
                continue
        with self.lock:
            self.sessions, self.windows, self.at = clean, windows, time.monotonic()

    def snapshot(self):
        with self.lock:
            return [] if time.monotonic() - self.at > STALE else list(self.sessions)

    def layout(self):
        with self.lock:
            return [] if time.monotonic() - self.at > STALE else list(self.windows)

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


# ---- cached text --------------------------------------------------------------------------------
# The board redraws the same titles and labels every frame; rasterising them was ~70% of the frame
# time. Draw keeps each (font, text, anchor) as a ready glyph mask and only blits it.
class Draw(ImageDraw.ImageDraw):
    def textlength(self, text, font=None, *a, **k):
        if a or k or font is None or not isinstance(text, str):
            return super().textlength(text, font, *a, **k)
        return _length(font, text)

    def text(self, xy, text, fill=None, font=None, anchor=None, *a, **k):
        if a or k or font is None or not isinstance(text, str) or "\n" in text:
            return super().text(xy, text, fill, font, anchor, *a, **k)
        m, ox, oy = _glyphs(font, text, anchor)
        if m is not None:
            self.bitmap((round(xy[0]) + ox, round(xy[1]) + oy), m, fill)


@functools.lru_cache(maxsize=4096)
def _length(f, text):
    return f.getlength(text)


@functools.lru_cache(maxsize=2048)
def _glyphs(f, text, anchor):
    """(mask, dx, dy): text drawn at (x, y) with this anchor covers mask placed at (x + dx, y + dy)."""
    x0, y0, x1, y1 = f.getbbox(text, anchor=anchor)
    if x1 <= x0 or y1 <= y0:
        return None, 0, 0
    p = 2                                           # anti-aliasing can spill past the box
    m = Image.new("L", (x1 - x0 + 2 * p, y1 - y0 + 2 * p))
    ImageDraw.Draw(m).text((p - x0, p - y0), text, fill=255, font=f, anchor=anchor)
    return m, x0 - p, y0 - p


def fps(sessions):
    """Frame rate for the board: smooth only while something is working."""
    return FPS if any(s["state"] == "working" for s in sessions) else CALM_FPS


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
    d = Draw(img)
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
    d = Draw(img)
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
    return {"app": "Claude app", "vscode": "VS Code", "ptyxis": "Ptyxis", "kitty": "kitty",
            "terminal": "terminal", "other": "elsewhere"}.get(s["where"], s["where"] or "—")


ORDER = {"needs": 0, "done": 1, "working": 2, "idle": 3}


def ordered(sessions):
    return sorted(sessions, key=lambda s: (ORDER[s["state"]], s["since"] if s["state"] != "done" else -s["since"]))


# ---- the full Claude view ---------------------------------------------------------------------
def render(W, H, sessions, g, c, t=None, tmux=None):
    """sessions as pushed by the agent; g, c: GPU and CPU readings of the dashboard (may be None);
    tmux: the window/pane layout pushed by the agent (drawn as a map when present)."""
    t = time.time() if t is None else t
    img = Image.new("RGBA", (W, H), BG + (255,))
    d = Draw(img)
    pad = 26
    ss = ordered(sessions)
    _header(img, d, W, pad, ss, g, c, t)
    top, bottom = 104, H - 20
    if not ss and not tmux:
        _empty(img, d, W, H, t)
        return img
    by_pos = {s["pos"]: s for s in ss if s["where"] == "tmux" and s["pos"]}
    lx = pad
    if tmux:
        mw = int((W - 2 * pad) * 0.56)
        _map(img, d, pad, top, mw, bottom - top, tmux, by_pos, t)
        lx = pad + mw + 22
    shown = [s for s in ss if s["state"] != "idle"]          # seen sessions stay out of the way
    _list(img, d, lx, top, W - pad - lx, bottom - top, shown, ss, t)
    return img


def _header(img, d, W, pad, ss, g, c, t):
    busy = any(s["state"] == "working" for s in ss)
    img.alpha_composite(spark(58, t, breathe=0.07 if busy else 0.0, spin=0.35 if busy else 0.08), (pad - 4, 20))
    fw = font("serif", 48, 600)
    d.text((pad + 64, 20), "Claude", font=fw, fill=INK)
    x = pad + 64 + d.textlength("Claude", font=fw) + 22
    n = {k: sum(s["state"] == k for s in ss) for k in ORDER}
    parts = [(f"{n['needs']} need{'s' if n['needs'] == 1 else ''} you", CLAUDE) if n["needs"] else None,
             (f"{n['working']} working", INK2) if n["working"] else None,
             (f"{n['done']} done", DONE) if n["done"] else None]
    parts = [p for p in parts if p] or [("all quiet" if ss else "no sessions", MUTED)]
    fs = font("sans", 24, 600)
    for i, (txt, col) in enumerate(parts):
        if i:
            d.text((x, 48), "·", font=fs, fill=MUTED, anchor="lm"); x += 18
        d.text((x, 48), txt, font=fs, fill=col, anchor="lm")
        x += d.textlength(txt, font=fs) + 14
    clock = time.strftime("%H:%M")
    fc = font("sans", 38, 600)
    d.text((W - pad, 48), clock, font=fc, fill=INK, anchor="rm")
    x = W - pad - d.textlength(clock, font=fc) - 22
    fl, fv = font("sans", 18, 500), font("sans", 22, 650)
    for label, val in reversed(_gpu_strip(g, c)):
        wl = d.textlength(label, font=fl) + (8 if label else 0)
        wv = d.textlength(val, font=fv)
        pw = int(wl + wv + 30)
        if x - pw < 560:
            break
        x -= pw
        img.alpha_composite(rrect(pw, 42, 21, CARD), (int(x), 27))
        if label:
            d.text((x + 15, 48), label, font=fl, fill=MUTED, anchor="lm")
        d.text((x + 15 + wl, 48), val, font=fv, fill=INK, anchor="lm")
        x -= 10


def mix(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


STATE_COL = {"needs": CLAUDE, "done": DONE, "working": CLAUDE, "idle": MUTED}
STATE_LABEL = {"needs": "NEEDS YOU", "done": "DONE", "working": "WORKING", "idle": "IDLE"}


def wrap(d, text, f, maxw, lines):
    """Up to `lines` lines of text, the last one shortened with an ellipsis."""
    words, out = text.split(), []
    while words and len(out) < lines:
        line = words.pop(0)
        while words and d.textlength(line + " " + words[0], font=f) <= maxw:
            line += " " + words.pop(0)
        if len(out) == lines - 1 and words:
            line = fit(d, line + " " + " ".join(words), f, maxw)
            words = []
        out.append(fit(d, line, f, maxw))
    return out


def comet(img, x, y, w, h, t, color, period=2.4):
    """A bright dot running around a tile's edge with a fading tail (the "working" animation)."""
    layer = Image.new("RGBA", (w + 16, h + 16), (0, 0, 0, 0))
    ld = Draw(layer)
    per = 2 * (w + h)
    N = 46
    for i in range(N):
        p = ((t / period) % 1.0 * per - i * per * 0.0045) % per
        if p < w: px, py = p, 0
        elif p < w + h: px, py = w, p - w
        elif p < 2 * w + h: px, py = w - (p - w - h), h
        else: px, py = 0, h - (p - 2 * w - h)
        px, py = min(max(px, 1), w - 1) + 8, min(max(py, 1), h - 1) + 8
        r = 3.6 * (1 - i / (N + 8))
        ld.ellipse([px - r, py - r, px + r, py + r], fill=color + (int(255 * (1 - i / N) ** 1.5),))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(1.2)), (x - 8, y - 8))


def _map(img, d, x, y, w, h, windows, by_pos, t):
    """The tmux windows, each with its panes laid out as on the terminal, coloured by state."""
    head = 34
    rows = [max(1, len({p["y"] for p in win["panes"]})) for win in windows]
    gap = 14
    avail = h - len(windows) * head - gap * (len(windows) - 1)
    hs = [max(96, int(avail * r / sum(rows))) for r in rows]
    while sum(hs) > avail and max(hs) > 96:
        i = hs.index(max(hs)); hs[i] -= 1
    cy = y
    for win, wh in zip(windows, hs):
        if cy + head + 60 > y + h:
            break
        wh = min(wh, y + h - cy - head)
        lab = f"WINDOW {win['win']}"
        lw = spaced(d, (x + 4, cy + 4), lab, font("sans", 17, 700), INK2 if win["active"] else MUTED, track=2.2)
        if win["name"] and not win["name"][:1].isdigit():
            d.text((x + 4 + lw + 12, cy + 2), fit(d, win["name"], font("sans", 19, 500), w - lw - 120),
                   font=font("sans", 19, 500), fill=MUTED)
        if win["active"]:
            d.text((x + w - 4, cy + 3), "on screen", font=font("sans", 17, 500), fill=MUTED, anchor="ra")
        sx, sy = w / win["w"], wh / win["h"]
        for p in win["panes"]:
            x0, y0 = x + p["x"] * sx, cy + head + p["y"] * sy
            x1, y1 = x + min(win["w"], p["x"] + p["w"] + 1) * sx, cy + head + min(win["h"], p["y"] + p["h"] + 1) * sy
            _tile(img, d, int(x0) + 3, int(y0) + 3, int(x1 - x0) - 6, int(y1 - y0) - 6, p, by_pos.get(p["pos"]), t)
        cy += head + wh + gap


def _tile(img, d, x, y, w, h, p, s, t):
    now = time.time()
    st = s["state"] if s else None
    r = 18
    if st is None:                                          # not a Claude pane
        img.alpha_composite(rrect(w, h, r, (35, 34, 32), (52, 50, 46), 1), (x, y))
        d.text((x + 18, y + 14), p["pos"], font=font("sans", 30, 700), fill=MUTED)
        d.text((x + 18, y + 54), p["cmd"] or "shell", font=font("sans", 21, 500), fill=(110, 107, 99))
        if p["focus"]:
            _here(d, x, y, p["pos"])
        return
    col = STATE_COL[st]
    if st == "needs":
        level = 3 + round(4 * (0.5 + 0.5 * math.sin(t * 2 * math.pi / 1.8)))
        img.alpha_composite(glow(w, h, r, CLAUDE, 12, level), (x - 24, y - 24))
        fill = mix(BG, CLAUDE, 0.24)
    elif st == "done":
        fill = mix(BG, DONE, 0.17)
    else:
        fill = CARD_HI if st == "working" else CARD
    img.alpha_composite(rrect(w, h, r, fill, col + ((200,) if st != "idle" else (60,)), 2), (x, y))
    if st == "working":
        comet(img, x, y, w, h, t + hash(s["id"]) % 10 * 0.23, CLAUDE)

    # top: position + state label
    d.text((x + 18, y + 12), p["pos"], font=font("sans", 32, 700), fill=INK if st != "idle" else INK2)
    spaced(d, (x + w - 18, y + 22), STATE_LABEL[st], font("sans", 17, 700), col, track=2.0, anchor_right=True)
    if p["focus"]:
        _here(d, x, y, p["pos"])

    # icon, bottom right
    ic = int(min(h * 0.42, w * 0.3, 96))
    icon_w = 0
    if ic >= 34:
        if st == "needs":
            m = mascot(max(2, ic // 17), t + hash(s["id"]) % 7 * 0.13, "wave")
        elif st == "done":
            ring = int((t * 6) % 9) if now - s["since"] < 90 else 0
            m = check_icon(ic, DONE, ring)
        elif st == "working":
            m = None
            fsz = int(ic * 0.9)
            d.text((x + w - 16 - fsz // 2, y + h - 14 - fsz // 2), spinner(t + hash(s["id"]) % 10 * 0.07),
                   font=font("sym", fsz), fill=CLAUDE, anchor="mm")
            icon_w = fsz
        else:
            m = mascot(max(2, ic // 20), t, "sleep" if now - s["since"] > 600 else "idle", color=(120, 116, 106))
        if m is not None:
            img.alpha_composite(m, (x + w - 14 - m.width, y + h - 12 - m.height))
            icon_w = m.width

    # title (wrapped) and time
    title = s["title"] or s["project"] or "Claude session"
    ft = font("serif", 27 if h >= 150 else 24, 600)
    ty, lh = y + 58, (35 if h >= 150 else 31)
    fwhen = font("sans", 20, 500)
    when_y = y + h - 34
    n_lines = max(0, min(3, (when_y - ty - 4) // lh))
    if n_lines == 0 and h >= 70:                            # short tile: title beside the position
        px = x + 18 + d.textlength(p["pos"], font=font("sans", 32, 700)) + 16
        lbl = d.textlength(STATE_LABEL[st], font=font("sans", 17, 700)) + 2 * len(STATE_LABEL[st])
        d.text((px, y + 18), fit(d, title, font("serif", 24, 600), x + w - 30 - lbl - px), font=font("serif", 24, 600),
               fill=INK if st != "idle" else INK2)
    else:
        for i, line in enumerate(wrap(d, title, ft, w - 36 - (icon_w if i_overlaps(ty, lh, n_lines, y, h, ic) else 0), n_lines)):
            d.text((x + 18, ty + i * lh), line, font=ft, fill=INK if st != "idle" else INK2)
    when = {"needs": "waiting " + ago(s["since"], now), "done": ago(s["since"], now) + " ago",
            "working": ago(s["since"], now), "idle": "idle " + ago(s["since"], now)}[st].replace("just now ago", "just now")
    if h >= 70:
        d.text((x + 18, when_y), fit(d, when + (f" · {s['project']}" if s["project"] else ""), fwhen, w - 50 - icon_w),
               font=fwhen, fill=col if st in ("needs", "done") else MUTED)


def i_overlaps(ty, lh, n, y, h, ic):
    return ty + n * lh > y + h - 12 - ic * 1.0


def _here(d, x, y, pos):
    """Marks the pane that has the keyboard: a small dot after its position."""
    px = x + 18 + d.textlength(pos, font=font("sans", 32, 700)) + 10
    d.ellipse([px, y + 30, px + 10, y + 40], fill=INK)


def _list(img, d, x, y, w, h, shown, ss, t):
    """Sessions that need attention or are busy (and the ones outside tmux), large and readable."""
    if not shown:                                           # nothing going on: a calm hero
        m = mascot(7 if w > 600 else 6, t, "idle")
        img.alpha_composite(m, (x + (w - m.width) // 2, y + h // 2 - m.height - 40))
        d.text((x + w // 2, y + h // 2), "All caught up", font=font("serif", 42, 600), fill=INK, anchor="ma")
        d.text((x + w // 2, y + h // 2 + 58), "Nothing needs you right now" if ss else "No Claude sessions yet",
               font=font("sans", 22, 500), fill=MUTED, anchor="ma")
        _idle_note(d, x, y + h - 30, w, y + h, ss)
        return
    gap = 12
    big = 128 if len(shown) <= 4 else 108
    hs = [big if s["state"] in ("needs", "done") else big - 20 for s in shown]
    n = len(shown)
    while n and sum(hs[:n]) + gap * (n - 1) > h:
        n -= 1
    if n < len(shown) and n:
        n -= 1 if sum(hs[:n]) + gap * n + 34 > h else 0
    cy = y
    for s, ih in zip(shown[:n], hs[:n]):
        _item(img, d, x, cy, w, ih, s, t)
        cy += ih + gap
    if n < len(shown):
        d.text((x + w // 2, cy + 2), f"+ {len(shown) - n} more", font=font("sans", 20, 600), fill=MUTED, anchor="ma")
        cy += 36
    _idle_note(d, x, cy, w, y + h, ss)


def _idle_note(d, x, y, w, bottom, ss):
    """One quiet line for the sessions you have already seen, by where they run."""
    idle = {}
    for s in ss:
        if s["state"] == "idle":
            k = "tmux" if s["where"] == "tmux" else place(s)
            idle[k] = idle.get(k, 0) + 1
    if not idle or y + 30 > bottom:
        return
    txt = "Seen · " + " · ".join(f"{n} {k}" for k, n in sorted(idle.items(), key=lambda kv: -kv[1]))
    f = font("sans", 20, 500)
    d.text((x + 8, max(y + 4, bottom - 30) if y < bottom - 60 else y + 4), fit(d, txt, f, w - 16), font=f, fill=MUTED)


def _item(img, d, x, y, w, h, s, t):
    st, now = s["state"], time.time()
    col = STATE_COL[st]
    if st == "needs":
        level = 3 + round(4 * (0.5 + 0.5 * math.sin(t * 2 * math.pi / 1.8)))
        img.alpha_composite(glow(w, h, 18, CLAUDE, 10, level), (x - 20, y - 20))
    fill = {"needs": mix(BG, CLAUDE, 0.22), "done": mix(BG, DONE, 0.14)}.get(st, CARD)
    img.alpha_composite(rrect(w, h, 18, fill), (x, y))
    img.alpha_composite(rrect(6, h - 32, 3, col), (x + 12, y + 16))
    cy = y + h // 2
    # icon column
    if st == "needs":
        m = mascot(3, t + hash(s["id"]) % 7 * 0.13, "wave")
        img.alpha_composite(m, (x + 30, cy - m.height // 2))
    elif st == "done":
        img.alpha_composite(check_icon(56, DONE, int((t * 6) % 9) if now - s["since"] < 90 else 0), (x + 28, cy - 28))
    elif st == "working":
        d.text((x + 56, cy), spinner(t + hash(s["id"]) % 10 * 0.07), font=font("sym", 40), fill=CLAUDE, anchor="mm")
    else:
        d.ellipse([x + 49, cy - 7, x + 63, cy + 7], fill=MUTED)
    tx = x + 96
    title = s["title"] or s["project"] or "Claude session"
    ft = font("serif", 28, 600)
    lines = wrap(d, title, ft, x + w - 20 - tx, 2 if h >= 120 else 1)
    th = 36 * len(lines)
    ty = cy - (th + 40) // 2
    for i, line in enumerate(lines):
        d.text((tx, ty + i * 36), line, font=ft, fill=INK)
    my = ty + th + 6
    cw = _chip(img, d, tx, my, place(s), col if st != "idle" else MUTED)
    when = {"needs": "waiting " + ago(s["since"], now), "done": ago(s["since"], now) + " ago",
            "working": "working " + ago(s["since"], now), "idle": "idle " + ago(s["since"], now)}[st]
    when = when.replace("just now ago", "just now")
    fm = font("sans", 20, 500)
    d.text((tx + cw + 12, my + 17), fit(d, when + (f" · {s['project']}" if s["project"] else ""), fm, x + w - 20 - tx - cw - 12),
           font=fm, fill=col if st in ("needs", "done") else MUTED, anchor="lm")


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


def _chip(img, d, x, y, text, col):
    f = font("sans", 19, 650)
    w = int(d.textlength(text, font=f) + 24)
    img.alpha_composite(rrect(w, 34, 17, col + (46,)), (int(x), int(y)))
    d.text((x + w / 2, y + 17), text, font=f, fill=col, anchor="mm")
    return w


# ---- small badge on the GPU dashboard ----------------------------------------------------------
def badge(img, right, y, sessions, t):
    """'✻ 2 working' (or 'Claude · 3 idle') next to the dashboard clock; returns nothing."""
    if not sessions:
        return
    d = Draw(img)
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
    d = Draw(img)
    d.text((x + 46, y + 19), text, font=f, fill=INK if n else INK2, anchor="lm")
