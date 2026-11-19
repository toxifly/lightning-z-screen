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
import collections, functools, math, os, threading, time
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
DEJAVU = "/usr/share/fonts/truetype/dejavu/"
FPS = 12                     # frame rate while a session is working (cached 2 s loop)
CALM_FPS = 2                 # frame rate otherwise: needs/done/idle tiles barely move
LOOP = 2.0                   # seconds per harmonized animation cycle (all motions divide this)
LOOP_STEPS = int(LOOP * FPS) # 24 discrete frames per cycle (2 fps visits 4 of them)
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
    return _fit(text, f, round(maxw))


@functools.lru_cache(maxsize=2048)
def _fit(text, f, maxw):
    if _length(f, text) <= maxw:
        return text
    while text and _length(f, text + "…") > maxw:
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


def _loop_slot(t):
    return int(((t % LOOP) / LOOP) * LOOP_STEPS) % LOOP_STEPS


# Claude's spark: 6-fold symmetric alternating rays so a 60° rotation over LOOP loops seamlessly
RAYS = tuple((k * 60 + d, ln) for k in range(6) for d, ln in ((0, 0.98), (31, 0.78)))


@functools.lru_cache(maxsize=64)
def _spark_frame(size, color, breathe, slot):
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = Draw(img)
    c, R, w0 = S / 2, S * 0.48, S * 0.095
    rot = math.radians(60.0 * slot / LOOP_STEPS)
    bphase = 2 * math.pi * slot / LOOP_STEPS
    for i, (deg, ln) in enumerate(RAYS):
        a = math.radians(deg) + rot
        L = min(R, R * ln * (1 + breathe * math.sin(bphase + (i % 2) * math.pi)))
        ca, sa = math.cos(a), math.sin(a)
        px, py = -sa, ca
        x0, y0, x1, y1 = c + ca * S * 0.05, c + sa * S * 0.05, c + ca * (L - w0 * 0.3), c + sa * (L - w0 * 0.3)
        w1 = w0 * 0.62
        d.polygon([(x0 + px * w0 / 2, y0 + py * w0 / 2), (x1 + px * w1 / 2, y1 + py * w1 / 2),
                   (x1 - px * w1 / 2, y1 - py * w1 / 2), (x0 - px * w0 / 2, y0 - py * w0 / 2)], fill=color)
        d.ellipse([x1 - w1 / 2, y1 - w1 / 2, x1 + w1 / 2, y1 + w1 / 2], fill=color)
    d.ellipse([c - w0, c - w0, c + w0, c + w0], fill=color)
    return img.resize((size, size), Image.LANCZOS)


def spark(size, t, color=CLAUDE, breathe=0.0, spin=0.12):
    return _spark_frame(size, color, breathe, _loop_slot(t) if spin else 0)


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


def _mascot_slot_state(slot, mood):
    if mood == "sleep":
        return True, False, 0
    blink = slot < 2
    wave = mood == "wave" and bool((slot // 3) % 2)
    if mood == "hop":
        m = slot % 12
        lift = 2 if m < 4 else (1 if m < 6 else 0)
    elif mood == "wave":
        lift = (slot // 4) % 2
    elif mood == "idle":
        lift = (slot // 12) % 2
    else:
        lift = 0
    return blink, wave, lift


@functools.lru_cache(maxsize=128)
def _mascot_sprite(scale, color, blink, wave, lift):
    g = [row[:] for row in GRID]
    if blink:
        for y, x in EYES:
            g[y][x] = 1
    if wave:
        g[2][ARM_R] = 0
        g[1][ARM_R] = g[0][ARM_R] = 1
    h, w = len(g), len(g[0])
    small = Image.new("RGBA", (w, h + 2), (0, 0, 0, 0))
    for y in range(h):
        for x in range(w):
            if g[y][x]:
                small.putpixel((x, y + 2 - lift), color + (255,))
    # terminal cells are about twice as tall as wide: each banner pixel is 1 x 2
    return small.resize((w * scale, (h + 2) * scale * 2), Image.NEAREST)


def mascot(scale, t, mood="idle", color=CLAUDE):
    """Pixel mascot. mood: "wave" (needs you), "hop" (finished), "idle", "sleep"."""
    return _mascot_sprite(scale, color, *_mascot_slot_state(_loop_slot(t), mood))


SPIN = "·✢✳✶✻✽"
SPIN_FRAMES = SPIN + SPIN[::-1]   # 12 steps -> 6 Hz over the 2 s master loop


def spinner(t):
    return SPIN_FRAMES[(_loop_slot(t) // 2) % len(SPIN_FRAMES)]


def ago(since, now):
    s = max(0, int(now - since))
    if s < 15: return "just now"
    if s < 60: return "< 1 min"
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
# Static elements (background, cards, glows per pulse level, wrapped titles, chips, window headers)
# are rendered once into an RGB backdrop; each frame copies the backdrop and only pastes the moving
# bits (spark, comet, spinners, mascot, check halo, elapsed time, GPU strip).
_BODY_CACHE = collections.OrderedDict()
_BODY_CACHE_MAX = 12


def _paste(dst, src, xy):
    dst.paste(src, xy, src)


def _glow_level(slot):
    return 3 + round(4 * (0.5 + 0.5 * math.sin(2 * math.pi * slot / LOOP_STEPS)))


def _body_key(W, H, ss, tmux):
    sk = tuple((s["id"], s["title"], s["project"], s["pos"], s["where"], s["state"]) for s in ss)
    tk = (tuple((w["win"], w["name"], w["active"], w["w"], w["h"],
                 tuple((p["pos"], p["x"], p["y"], p["w"], p["h"], p["cmd"], p["focus"]) for p in w["panes"]))
                for w in tmux)
          if tmux else None)
    return (W, H, sk, tk)


def _get_body(bkey, level, W, H, ss, tmux):
    k = (bkey, level)
    hit = _BODY_CACHE.get(k)
    if hit is not None:
        _BODY_CACHE.move_to_end(k)
        return hit
    val = _build_body(W, H, ss, tmux, level)
    _BODY_CACHE[k] = val
    while len(_BODY_CACHE) > _BODY_CACHE_MAX:
        _BODY_CACHE.popitem(last=False)
    return val


def _build_body(W, H, ss, tmux, glow_level):
    img = Image.new("RGB", (W, H), BG)
    d = Draw(img)
    pad = 26
    slots = []
    _header_left(d, pad, ss)
    top, bottom = 104, H - 20
    if not ss and not tmux:
        m = _mascot_sprite(11, CLAUDE, True, False, 0)
        _paste(img, m, ((W - m.width) // 2, H // 2 - 170))
        d.text((W // 2, H // 2 + 20), "No Claude sessions", font=font("serif", 40, 500), fill=INK, anchor="ma")
        d.text((W // 2, H // 2 + 78), "Start one in tmux and it will show up here", font=font("sans", 21, 400),
               fill=MUTED, anchor="ma")
        slots.append(("empty_z", W, H, m.width))
        return img, slots
    by_pos = {s["pos"]: i for i, s in enumerate(ss) if s["where"] == "tmux" and s["pos"]}
    lx = pad
    if tmux:
        mw = int((W - 2 * pad) * 0.56)
        _map(img, d, pad, top, mw, bottom - top, tmux, by_pos, ss, glow_level, slots)
        lx = pad + mw + 22
    shown = [(i, s) for i, s in enumerate(ss) if s["state"] != "idle"]   # seen sessions stay out of the way
    _list(img, d, lx, top, W - pad - lx, bottom - top, shown, ss, glow_level, slots)
    return img, slots


def _when_text(st, since, now, project, is_tile):
    a = ago(since, now)
    if st == "needs":
        w = "waiting " + a
    elif st == "done":
        w = "just now" if a == "just now" else a + " ago"
    elif st == "working":
        w = a if is_tile else "working " + a
    else:
        w = "idle " + a
    return w + (f" · {project}" if project else "")


def plan(W, H, sessions, g, c, t=None, tmux=None):
    """Return (frame_key, draw_fn) for the board at time t.

    Because every animation is harmonized to the 2 s master loop (LOOP_STEPS = 24), frame_key
    repeats identically across cycles while sessions, clock and GPU strip stay steady — allowing
    the caller to cache encoded JPEGs directly by frame_key (0 ms render + 0 ms JPEG on hits)."""
    now = time.time()
    t = now if t is None else t
    pad = 26
    slot = _loop_slot(t)
    ss = ordered(sessions)
    busy = any(s["state"] == "working" for s in ss)
    level = _glow_level(slot) if any(s["state"] == "needs" for s in ss) else None
    bkey = _body_key(W, H, ss, tmux)
    clock = time.strftime("%H:%M")
    gstrip = tuple(_gpu_strip(g, c))
    sp_key = (58, CLAUDE, 0.07 if busy else 0.0, slot if busy else 0)

    base, slots = _get_body(bkey, level, W, H, ss, tmux)
    dyn_key = []
    ops = []
    for sl in slots:
        kind = sl[0]
        if kind == "comet":
            _, x, y, w, h, off = sl
            step = (slot + off) % LOOP_STEPS
            dyn_key.append(step)
            ops.append(("comet", x, y, w, h, step))
        elif kind == "tile_icon":
            _, x, y, w, h, ic, st, idx, sid_h = sl
            s = ss[idx]
            off = sid_h % LOOP_STEPS
            if st == "needs":
                sc = max(2, ic // 17)
                ms = _mascot_slot_state((slot + off) % LOOP_STEPS, "wave")
                dyn_key.append(ms)
                ops.append(("mascot_br", x + w - 14, y + h - 12, sc, CLAUDE, ms))
            elif st == "done":
                m = slot // 2
                ring = (m if m < 9 else 0) if now - s["since"] < 90 else 0
                dyn_key.append(ring)
                ops.append(("check_br", x + w - 14, y + h - 12, ic, ring))
            elif st == "working":
                ch = SPIN_FRAMES[((slot + off) // 2) % len(SPIN_FRAMES)]
                fsz = int(ic * 0.9)
                dyn_key.append(ch)
                ops.append(("text", (x + w - 16 - fsz // 2, y + h - 14 - fsz // 2), ch, font("sym", fsz), CLAUDE, "mm"))
            else:
                sc = max(2, ic // 20)
                mood = "sleep" if now - s["since"] > 600 else "idle"
                ms = _mascot_slot_state(slot, mood)
                dyn_key.append(ms)
                ops.append(("mascot_br", x + w - 14, y + h - 12, sc, (120, 116, 106), ms))
        elif kind == "when":
            _, tx, ty, maxw, anchor, fill, st, idx, is_tile = sl
            s = ss[idx]
            txt = _when_text(st, s["since"], now, s["project"], is_tile)
            fwhen = font("sans", 20, 500)
            fitted = _fit(txt, fwhen, round(maxw))
            dyn_key.append(fitted)
            ops.append(("text", (tx, ty), fitted, fwhen, fill, anchor))
        elif kind == "item_icon":
            _, x, cy, st, idx, sid_h = sl
            s = ss[idx]
            off = sid_h % LOOP_STEPS
            if st == "needs":
                ms = _mascot_slot_state((slot + off) % LOOP_STEPS, "wave")
                dyn_key.append(ms)
                ops.append(("mascot_item", x + 30, cy, 3, CLAUDE, ms))
            elif st == "done":
                m = slot // 2
                ring = (m if m < 9 else 0) if now - s["since"] < 90 else 0
                dyn_key.append(ring)
                ops.append(("check_xy", x + 28, cy - 28, 56, ring))
            elif st == "working":
                ch = SPIN_FRAMES[((slot + off) // 2) % len(SPIN_FRAMES)]
                dyn_key.append(ch)
                ops.append(("text", (x + 56, cy), ch, font("sym", 40), CLAUDE, "mm"))
        elif kind == "hero_mascot":
            _, x, y, w, h = sl
            sc = 7 if w > 600 else 6
            ms = _mascot_slot_state(slot, "idle")
            dyn_key.append(ms)
            ops.append(("mascot_hero", x, y, w, h, sc, ms))
        elif kind == "empty_z":
            _, ew, eh, mw = sl
            zs = tuple((slot + i * (LOOP_STEPS // 3)) % LOOP_STEPS for i in range(3))
            dyn_key.append(zs)
            ops.append(("empty_z", ew, eh, mw, zs))

    frame_key = (bkey, level, clock, gstrip, sp_key, tuple(dyn_key))

    def draw():
        img = base.copy()
        d = Draw(img)
        strip, sx, sy = _header_right(W, pad, clock, gstrip)
        img.paste(strip, (sx, sy))
        _paste(img, _spark_frame(*sp_key), (pad - 4, 20))
        for op in ops:
            k = op[0]
            if k == "comet":
                _, x, y, w, h, step = op
                sp, ox, oy = _comet_sprite(w, h, CLAUDE, step)
                _paste(img, sp, (x + ox, y + oy))
            elif k == "mascot_br":
                _, rx, by, sc, col, ms = op
                m = _mascot_sprite(sc, col, *ms)
                _paste(img, m, (rx - m.width, by - m.height))
            elif k == "check_br":
                _, rx, by, ic, ring = op
                m = check_icon(ic, DONE, ring)
                _paste(img, m, (rx - m.width, by - m.height))
            elif k == "mascot_item":
                _, ix, cy, sc, col, ms = op
                m = _mascot_sprite(sc, col, *ms)
                _paste(img, m, (ix, cy - m.height // 2))
            elif k == "check_xy":
                _, ix, iy, sz, ring = op
                _paste(img, check_icon(sz, DONE, ring), (ix, iy))
            elif k == "mascot_hero":
                _, x, y, w, h, sc, ms = op
                m = _mascot_sprite(sc, CLAUDE, *ms)
                _paste(img, m, (x + (w - m.width) // 2, y + h // 2 - m.height - 40))
            elif k == "text":
                _, xy, txt, f, fill, anchor = op
                d.text(xy, txt, font=f, fill=fill, anchor=anchor)
            elif k == "empty_z":
                _, ew, eh, mw, zs = op
                for step in zs:
                    p = step / float(LOOP_STEPS)
                    f = font("serif", int(22 + 18 * p), 500)
                    a = (1 - p) * min(1, p * 4)
                    zx, zy = ew // 2 + mw // 2 - 10 + int(40 * p), eh // 2 - 170 - int(90 * p)
                    d.text((zx, zy), "z", font=f, fill=mix(BG, MUTED, a))
        return img

    return frame_key, draw


def render_if_changed(prev_key, W, H, sessions, g, c, t=None, tmux=None):
    """Render the board, or return (None, key) if nothing visible changed since prev_key."""
    key, draw = plan(W, H, sessions, g, c, t=t, tmux=tmux)
    if prev_key is not None and key == prev_key:
        return None, key
    return draw(), key


def render(W, H, sessions, g, c, t=None, tmux=None):
    """sessions as pushed by the agent; g, c: GPU and CPU readings of the dashboard (may be None);
    tmux: the window/pane layout pushed by the agent (drawn as a map when present)."""
    return render_if_changed(None, W, H, sessions, g, c, t=t, tmux=tmux)[0]


def _header_left(d, pad, ss):
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


@functools.lru_cache(maxsize=16)
def _header_right(W, pad, clock, gstrip):
    fc = font("sans", 38, 600)
    fl, fv = font("sans", 18, 500), font("sans", 22, 650)
    cw = _length(fc, clock)
    x = W - pad - cw - 22
    pills = []
    min_x = W - pad - cw - 4
    for label, val in reversed(gstrip):
        wl = _length(fl, label) + (8 if label else 0)
        wv = _length(fv, val)
        pw = int(wl + wv + 30)
        if x - pw < 560:
            break
        x -= pw
        pills.append((x, pw, wl, label, val))
        min_x = x
        x -= 10
    x0 = max(0, int(min_x) - 2)
    x1 = min(W, W - pad + 4)
    y0, h = 20, 56
    strip = Image.new("RGB", (max(1, x1 - x0), h), BG)
    d = Draw(strip)
    d.text((W - pad - x0, 48 - y0), clock, font=fc, fill=INK, anchor="rm")
    for px, pw, wl, label, val in pills:
        _paste(strip, rrect(pw, 42, 21, CARD), (int(px) - x0, 27 - y0))
        if label:
            d.text((px - x0 + 15, 48 - y0), label, font=fl, fill=MUTED, anchor="lm")
        d.text((px - x0 + 15 + wl, 48 - y0), val, font=fv, fill=INK, anchor="lm")
    return strip, x0, y0


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


COMET_STEPS = LOOP_STEPS     # 24 perimeter steps per 2 s loop (1 step per frame at 12 fps)


def _comet_step(t, period=LOOP):
    return int(((t / period) % 1.0) * COMET_STEPS) % COMET_STEPS


@functools.lru_cache(maxsize=512)
def _comet_sprite(w, h, color, step):
    """Tight bounding-box sprite for one quantized comet position around a (w x h) tile."""
    per = 2 * (w + h)
    N = 46
    base = (step / COMET_STEPS) * per
    pts = []
    for i in range(N):
        p = (base - i * per * 0.0045) % per
        if p < w: px, py = p, 0.0
        elif p < w + h: px, py = float(w), p - w
        elif p < 2 * w + h: px, py = w - (p - w - h), float(h)
        else: px, py = 0.0, h - (p - 2 * w - h)
        px, py = min(max(px, 1.0), w - 1.0), min(max(py, 1.0), h - 1.0)
        r = 3.6 * (1 - i / (N + 8))
        pts.append((px, py, r, int(255 * (1 - i / N) ** 1.5)))
    bx0 = max(-8, int(min(px - r for px, py, r, _ in pts)) - 5)
    by0 = max(-8, int(min(py - r for px, py, r, _ in pts)) - 5)
    bx1 = min(w + 8, int(math.ceil(max(px + r for px, py, r, _ in pts))) + 5)
    by1 = min(h + 8, int(math.ceil(max(py + r for px, py, r, _ in pts))) + 5)
    layer = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for px, py, r, a in pts:
        ld.ellipse([px - bx0 - r, py - by0 - r, px - bx0 + r, py - by0 + r], fill=color + (a,))
    return layer.filter(ImageFilter.GaussianBlur(1.2)), bx0, by0


def comet(img, x, y, w, h, t, color, period=2.4):
    """A bright dot running around a tile's edge with a fading tail (the "working" animation)."""
    sp, ox, oy = _comet_sprite(w, h, color, _comet_step(t, period))
    if img.mode == "RGBA":
        img.alpha_composite(sp, (x + ox, y + oy))
    else:
        _paste(img, sp, (x + ox, y + oy))


def _map(img, d, x, y, w, h, windows, by_pos, ss, glow_level, slots):
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
            idx = by_pos.get(p["pos"])
            _tile(img, d, int(x0) + 3, int(y0) + 3, int(x1 - x0) - 6, int(y1 - y0) - 6,
                  p, idx, ss[idx] if idx is not None else None, glow_level, slots)
        cy += head + wh + gap


def _tile(img, d, x, y, w, h, p, idx, s, glow_level, slots):
    st = s["state"] if s else None
    r = 18
    if st is None:                                          # not a Claude pane
        _paste(img, rrect(w, h, r, (35, 34, 32), (52, 50, 46), 1), (x, y))
        d.text((x + 18, y + 14), p["pos"], font=font("sans", 30, 700), fill=MUTED)
        d.text((x + 18, y + 54), p["cmd"] or "shell", font=font("sans", 21, 500), fill=(110, 107, 99))
        if p["focus"]:
            _here(d, x, y, p["pos"])
        return
    col = STATE_COL[st]
    if st == "needs":
        _paste(img, glow(w, h, r, CLAUDE, 12, glow_level), (x - 24, y - 24))
        fill = mix(BG, CLAUDE, 0.24)
    elif st == "done":
        fill = mix(BG, DONE, 0.17)
    else:
        fill = CARD_HI if st == "working" else CARD
    _paste(img, rrect(w, h, r, fill, col + ((200,) if st != "idle" else (60,)), 2), (x, y))
    sid_h = hash(s["id"])
    if st == "working":
        slots.append(("comet", x, y, w, h, sid_h % LOOP_STEPS))

    # top: position + state label
    d.text((x + 18, y + 12), p["pos"], font=font("sans", 32, 700), fill=INK if st != "idle" else INK2)
    spaced(d, (x + w - 18, y + 22), STATE_LABEL[st], font("sans", 17, 700), col, track=2.0, anchor_right=True)
    if p["focus"]:
        _here(d, x, y, p["pos"])

    # icon, bottom right (drawn dynamically on top of the backdrop)
    ic = int(min(h * 0.42, w * 0.3, 96))
    icon_w = 0
    if ic >= 34:
        if st == "needs":
            icon_w = len(GRID[0]) * max(2, ic // 17)
        elif st == "done":
            icon_w = ic
        elif st == "working":
            icon_w = int(ic * 0.9)
        else:
            icon_w = len(GRID[0]) * max(2, ic // 20)
        slots.append(("tile_icon", x, y, w, h, ic, st, idx, sid_h))

    # title (wrapped) and time
    title = s["title"] or s["project"] or "Claude session"
    ft = font("serif", 27 if h >= 150 else 24, 600)
    ty, lh = y + 58, (35 if h >= 150 else 31)
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
    if h >= 70:
        slots.append(("when", x + 18, when_y, w - 50 - icon_w, None, col if st in ("needs", "done") else MUTED, st, idx, True))


def i_overlaps(ty, lh, n, y, h, ic):
    return ty + n * lh > y + h - 12 - ic * 1.0


def _here(d, x, y, pos):
    """Marks the pane that has the keyboard: a small dot after its position."""
    px = x + 18 + d.textlength(pos, font=font("sans", 32, 700)) + 10
    d.ellipse([px, y + 30, px + 10, y + 40], fill=INK)


def _list(img, d, x, y, w, h, shown, ss, glow_level, slots):
    """Sessions that need attention or are busy (and the ones outside tmux), large and readable."""
    if not shown:                                           # nothing going on: a calm hero
        slots.append(("hero_mascot", x, y, w, h))
        d.text((x + w // 2, y + h // 2), "All caught up", font=font("serif", 42, 600), fill=INK, anchor="ma")
        d.text((x + w // 2, y + h // 2 + 58), "Nothing needs you right now" if ss else "No Claude sessions yet",
               font=font("sans", 22, 500), fill=MUTED, anchor="ma")
        _idle_note(d, x, y + h - 30, w, y + h, ss)
        return
    gap = 12
    big = 128 if len(shown) <= 4 else 108
    hs = [big if s["state"] in ("needs", "done") else big - 20 for _, s in shown]
    n = len(shown)
    while n and sum(hs[:n]) + gap * (n - 1) > h:
        n -= 1
    if n < len(shown) and n:
        n -= 1 if sum(hs[:n]) + gap * n + 34 > h else 0
    cy = y
    for (idx, s), ih in zip(shown[:n], hs[:n]):
        _item(img, d, x, cy, w, ih, idx, s, glow_level, slots)
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


def _item(img, d, x, y, w, h, idx, s, glow_level, slots):
    st = s["state"]
    col = STATE_COL[st]
    if st == "needs":
        _paste(img, glow(w, h, 18, CLAUDE, 10, glow_level), (x - 20, y - 20))
    fill = {"needs": mix(BG, CLAUDE, 0.22), "done": mix(BG, DONE, 0.14)}.get(st, CARD)
    _paste(img, rrect(w, h, 18, fill), (x, y))
    _paste(img, rrect(6, h - 32, 3, col), (x + 12, y + 16))
    cy = y + h // 2
    if st in ("needs", "done", "working"):
        slots.append(("item_icon", x, cy, st, idx, hash(s["id"])))
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
    slots.append(("when", tx + cw + 12, my + 17, x + w - 20 - tx - cw - 12, "lm",
                  col if st in ("needs", "done") else MUTED, st, idx, False))


def _gpu_strip(g, c):
    out = []
    if g:
        if g.get("temp") is not None: out.append(("GPU", f"{g['temp']:.0f}°C"))
        if g.get("power") is not None: out.append(("", f"{g['power']:.0f} W"))
        if g.get("load") is not None: out.append(("load", f"{g['load']:.0f}%"))
    if c and c.get("temp") is not None:
        out.append(("CPU", f"{c['temp']:.0f}°C"))
    return out


def _chip(img, d, x, y, text, col):
    f = font("sans", 19, 650)
    w = int(d.textlength(text, font=f) + 24)
    _paste(img, rrect(w, 34, 17, col + (46,)), (int(x), int(y)))
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
    _paste(img, rrect(w, 38, 19, CARD_HI), (x, y))
    sp = spark(26, t, breathe=0.08 if n else 0, spin=0.4 if n else 0.05)
    _paste(img, sp, (x + 12, y + 6))
    d.text((x + 46, y + 19), text, font=f, fill=INK if n else INK2, anchor="lm")
