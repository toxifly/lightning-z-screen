#!/usr/bin/env python3
"""GPU monitor for the MSI RTX 5090 Lightning Z on-card screen (1280x800, AiCast USB display).

Renders a dashboard with Pillow and streams it as JPEG frames. Data sources:
  * NVML (nvidia-ml-py): GPU temperature, load, VRAM, power and limit, clocks, fan
  * hwmon k10temp + /proc/stat: CPU temperature and load
  * Thermal Grizzly WireView Pro II units (optional): per-connector power and per-pin current

It can also show a web page or a video (modes.py), or act as a desktop monitor (monitor.py);
switch with lightning-screen.

Usage: dashboard.py [--rotate 0|90|180|270] [--fps 2] [--wireview NAME=DEV ...] [--preview out.png]
"""
import argparse, collections, glob, io, os, signal, sys, threading, time
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aicast, claude_board, control, hidctl, modes  # noqa: E402

# ---- palette (dataviz reference palette, dark mode) -------------------------------------------
SURFACE, CARD, TRACK = "#1a1a19", "#232321", "#383835"
INK, INK2, MUTED = "#f0efec", "#c3c2b7", "#8f8e86"
ACCENT = "#3987e5"                       # categorical slot 1 (dark)
GOOD, WARN, SERIOUS, CRIT = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
def font(size, bold=False):
    return ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)

F = dict(title=font(30, True), label=font(22), small=font(18), value=font(34, True),
         hero=font(96, True), unit=font(30), status=font(20, True))

# pin rating for 12V-2x6 is 9.5 A; warn early
PIN_WARN_A, PIN_CRIT_A = 8.0, 9.5
GPU_TEMP_WARN, GPU_TEMP_CRIT = 80, 88


# ---- data sources ----------------------------------------------------------------------------
class Gpu:
    def __init__(self):
        import pynvml as n
        self.n = n
        n.nvmlInit()
        self.h = n.nvmlDeviceGetHandleByIndex(0)
        self.name = n.nvmlDeviceGetName(self.h)
        if isinstance(self.name, bytes): self.name = self.name.decode()

    def read(self):
        n, h = self.n, self.h
        def safe(f, *a, default=None):
            try: return f(*a)
            except n.NVMLError: return default
        mem = safe(n.nvmlDeviceGetMemoryInfo, h)
        return dict(
            temp=safe(n.nvmlDeviceGetTemperature, h, n.NVML_TEMPERATURE_GPU),
            load=getattr(safe(n.nvmlDeviceGetUtilizationRates, h), "gpu", None),
            mem_used=mem.used / 2**30 if mem else None, mem_total=mem.total / 2**30 if mem else None,
            power=(safe(n.nvmlDeviceGetPowerUsage, h) or 0) / 1000,
            limit=(safe(n.nvmlDeviceGetEnforcedPowerLimit, h) or 0) / 1000,
            core=safe(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_GRAPHICS),
            memclk=safe(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_MEM),
            fan=safe(n.nvmlDeviceGetFanSpeed, h),
        )


class Cpu:
    def __init__(self):
        self.temp_path = None
        for d in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                if open(d + "/name").read().strip() == "k10temp":
                    for lab in glob.glob(d + "/temp*_label"):
                        if open(lab).read().strip() == "Tctl":
                            self.temp_path = lab.replace("_label", "_input")
            except OSError:
                pass
        self.prev = self._stat()

    @staticmethod
    def _stat():
        v = list(map(int, open("/proc/stat").readline().split()[1:]))
        return sum(v), v[3] + v[4]

    def read(self):
        tot, idle = self._stat()
        dt, di = tot - self.prev[0], idle - self.prev[1]
        self.prev = (tot, idle)
        load = 100 * (1 - di / dt) if dt else None
        temp = int(open(self.temp_path).read()) / 1000 if self.temp_path else None
        return dict(load=load, temp=temp)


class WireViews:
    """Background reader; the WireView is a serial device, so poll gently (once a second)."""
    def __init__(self, devs):
        self.devs, self.data = devs, {}
        try:
            import wvread
        except ImportError:
            return
        self.wv = wvread
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        wv = self.wv
        while True:
            for name, dev in self.devs:
                s = None
                if os.path.exists(dev):
                    try:
                        fd = wv.open_port(dev, wait_s=1.0)
                        try: s = wv.parse_sensor(wv.transact(fd, bytes([wv.CMD_READ_SENSOR_VALUES]), wv.SENSOR_SIZE))
                        finally: wv.close_port(fd)
                    except Exception:            # unplugged or busy (OSError, termios.error, ...)
                        s = None
                if s:
                    self.data[name] = dict(s, t=time.monotonic())
                elif name in self.data and time.monotonic() - self.data[name]["t"] > 5:
                    del self.data[name]
            time.sleep(1.0)


# ---- drawing helpers -------------------------------------------------------------------------
def card(d, box, title):
    d.rounded_rectangle(box, 14, fill=CARD)
    d.text((box[0] + 24, box[1] + 18), title, font=F["label"], fill=INK2)

def fmt(v, spec, dash="—"):
    return dash if v is None else format(v, spec)

def status_for(v, warn, crit):
    if v is None: return None
    if v >= crit: return ("critical", CRIT)
    if v >= warn: return ("high", WARN)
    return None

def badge(d, xy, st):
    """Status = icon + label, never colour alone."""
    if not st: return
    label, col = st
    x, y = xy
    d.polygon([(x, y + 22), (x + 12, y), (x + 24, y + 22)], fill=col)           # warning triangle
    d.text((x + 12, y + 13), "!", font=font(16, True), fill=SURFACE, anchor="mm")
    d.text((x + 32, y + 11), label, font=F["status"], fill=INK, anchor="lm")

def meter(d, x, y, w, name, frac, value_txt, col=ACCENT):
    d.text((x, y), name, font=F["label"], fill=INK2)
    d.text((x + w, y), value_txt, font=F["label"], fill=INK, anchor="ra")
    by = y + 34
    d.rounded_rectangle([x, by, x + w, by + 12], 6, fill=TRACK)
    if frac is not None and frac > 0:
        fx = x + max(12, int(w * min(frac, 1.0)))
        d.rounded_rectangle([x, by, fx, by + 12], 6, fill=col)

def sparkline(d, box, series, label, unit, lo=None, hi=None):
    x0, y0, x1, y1 = box
    d.text((x0, y0), label, font=F["small"], fill=INK2)
    vals = [v for v in series if v is not None]
    if not vals:
        return
    cur = vals[-1]
    d.text((x1, y0), f"{cur:.0f} {unit}", font=F["small"], fill=INK, anchor="ra")
    py0, py1 = y0 + 30, y1
    lo = min(vals) if lo is None else min(lo, min(vals))
    hi = max(vals) if hi is None else max(hi, max(vals))
    if hi - lo < 1e-6: hi = lo + 1
    d.line([(x0, py1), (x1, py1)], fill=TRACK, width=1)                        # recessive baseline
    n = len(series)
    pts = [(x0 + (x1 - x0) * i / max(n - 1, 1), py1 - (py1 - py0) * (v - lo) / (hi - lo))
           for i, v in enumerate(series) if v is not None]
    if len(pts) > 1:
        d.line(pts, fill=ACCENT, width=2, joint="curve")
    ex, ey = pts[-1]
    d.ellipse([ex - 5, ey - 5, ex + 5, ey + 5], fill=ACCENT, outline=CARD, width=2)


# ---- frame -----------------------------------------------------------------------------------
HIST = 120  # samples (~60 s at 2 fps)

def render(W, H, gpu_name, g, c, wv, hist):
    img = Image.new("RGB", (W, H), SURFACE)
    d = ImageDraw.Draw(img)
    pad = 24

    # header
    d.text((pad, 20), gpu_name.replace("NVIDIA GeForce ", "").upper() + "  LIGHTNING Z", font=F["title"], fill=INK)
    d.text((W - pad, 22), time.strftime("%H:%M"), font=F["title"], fill=INK2, anchor="ra")

    # hero card: GPU temperature and board power
    top = 76
    card(d, [pad, top, 620, top + 260], "GPU")
    t_st = status_for(g["temp"], GPU_TEMP_WARN, GPU_TEMP_CRIT)
    d.text((pad + 24, top + 60), fmt(g["temp"], ".0f"), font=F["hero"], fill=INK)
    tw = d.textlength(fmt(g["temp"], ".0f"), font=F["hero"])
    d.text((pad + 30 + tw, top + 74), "°C", font=F["unit"], fill=INK2)
    d.text((pad + 24, top + 176), "temperature", font=F["small"], fill=MUTED)
    badge(d, (pad + 24, top + 210), t_st)
    px = 330
    d.text((px, top + 60), fmt(g["power"], ".0f"), font=F["hero"], fill=INK)
    pw = d.textlength(fmt(g["power"], ".0f"), font=F["hero"])
    d.text((px + 6 + pw, top + 74), "W", font=F["unit"], fill=INK2)
    d.text((px, top + 176), "board power", font=F["small"], fill=MUTED)

    # meters card
    card(d, [640, top, W - pad, top + 260], "Load")
    mx, mw = 664, W - pad - 24 - 664
    meter(d, mx, top + 60, mw, "GPU", (g["load"] or 0) / 100, fmt(g["load"], ".0f") + " %")
    vram = (g["mem_used"] / g["mem_total"]) if g["mem_used"] is not None and g["mem_total"] else None
    meter(d, mx, top + 110, mw, "VRAM", vram, f"{fmt(g['mem_used'], '.1f')} / {fmt(g['mem_total'], '.0f')} GB")
    plim = g["power"] / g["limit"] if g["limit"] else None
    meter(d, mx, top + 160, mw, "Power limit", plim, f"{fmt(g['power'], '.0f')} / {fmt(g['limit'], '.0f')} W")
    meter(d, mx, top + 210, mw, "Fan", (g["fan"] or 0) / 100, fmt(g["fan"], ".0f") + " %")

    # stat tiles row
    ty, th = top + 280, 120
    tiles = [("Core clock", fmt(g["core"], ".0f"), "MHz"), ("Memory clock", fmt(g["memclk"], ".0f"), "MHz"),
             ("CPU", fmt(c["temp"], ".0f"), "°C"), ("CPU load", fmt(c["load"], ".0f"), "%")]
    tw_ = (W - 2 * pad - 3 * 20) / 4
    for i, (lab, val, unit) in enumerate(tiles):
        x = pad + i * (tw_ + 20)
        card(d, [x, ty, x + tw_, ty + th], lab)
        d.text((x + 24, ty + 58), val, font=F["value"], fill=INK)
        vw = d.textlength(val, font=F["value"])
        d.text((x + 32 + vw, ty + 68), unit, font=F["small"], fill=INK2)

    # bottom: connectors (WireView) + history
    by = ty + th + 20
    bh = H - pad - by
    cw = 620 - pad
    card(d, [pad, by, pad + cw, by + bh], "12V-2x6 connectors")
    if not wv:
        d.text((pad + 24, by + 70), "No WireView data", font=F["label"], fill=MUTED)
    else:
        for i, (name, s) in enumerate(wv.items()):
            yy = by + 62 + i * 92
            pins = [p[1] for p in s["pins"]]
            hi = max(pins)
            st = status_for(hi, PIN_WARN_A, PIN_CRIT_A)
            label = f"Connector {name.upper()}" if len(name) == 1 else f"{name.capitalize()} connector"
            d.text((pad + 24, yy), label, font=F["label"], fill=INK2)
            d.text((pad + cw - 24, yy), f"{s['total_w']:.0f} W", font=F["label"], fill=INK, anchor="ra")
            meter(d, pad + 24, yy + 34, cw - 48, "Hottest pin", hi / PIN_CRIT_A,
                  f"{hi:.2f} A  ·  {max(s['ts'][:2]):.0f} °C", col=(st[1] if st else ACCENT))
            if st: badge(d, (pad + 270, yy + 34), st)

    hx = 640
    card(d, [hx, by, W - pad, by + bh], "Last minute")
    sx0, sx1 = hx + 24, W - pad - 24
    mid = by + 50 + (bh - 60) / 2
    sparkline(d, [sx0, by + 54, sx1, mid - 12], hist["power"], "Board power", "W", lo=0)
    sparkline(d, [sx0, mid + 4, sx1, by + bh - 20], hist["temp"], "GPU temperature", "°C", lo=30)
    return img


class Dashboard:
    """The GPU monitor as a screen mode: renders a frame every 1/fps seconds.

    With a Claude board it doubles as the Claude sessions view: while a session is working, needs an
    answer or has finished unseen (and HOLD seconds after; or always, with force=True, i.e. the "claude" mode) it shows the board
    instead, animated at claude_board.fps() (smooth only while a session works), with the GPU numbers as a strip in its header."""
    done = False
    FADE = 0.5                           # seconds of cross-fade between the two views
    HOLD = 20                            # seconds the board stays up after the last active session

    def __init__(self, gpu, cpu, wv, hist, w, h, rotate, fps, quality, board=None):
        self.gpu, self.cpu, self.wv, self.hist = gpu, cpu, wv, hist
        self.size, self.rotate, self.period, self.quality = (w, h), rotate, 1.0 / fps, quality
        self.due = time.monotonic()
        self.board, self.force = board, False
        self.sample, self.sampled = None, 0.0
        self.view, self.last, self.fade = "gpu", None, None
        self.active_at = -1e9

    def read(self):
        """GPU and CPU readings, sampled (and added to the history) at most once per dashboard period."""
        now = time.monotonic()
        if self.sample is None or now - self.sampled >= self.period * 0.9:
            g, c = self.gpu.read(), self.cpu.read()
            self.hist["power"].append(g["power"]); self.hist["temp"].append(g["temp"])
            self.sample, self.sampled = (g, c), now
        return self.sample

    def _wh(self):
        return self.size[::-1] if self.rotate in (90, 270) else self.size

    def gpu_frame(self):
        g, c = self.read()
        W, H = self._wh()
        wv = {n: self.wv.data[n] for n, _ in self.wv.devs if n in self.wv.data} if self.wv else {}
        img = render(W, H, self.gpu.name, g, c, wv, self.hist)
        if self.board:
            clock_w = ImageDraw.Draw(img).textlength(time.strftime("%H:%M"), font=F["title"])
            claude_board.badge(img, W - 24 - clock_w - 20, 18, self.board.snapshot(), time.time())
        return img

    def claude_frame(self):
        g, c = self.read()
        W, H = self._wh()
        return claude_board.render(W, H, self.board.snapshot(), g, c, tmux=self.board.layout()).convert("RGB")

    def image(self):
        img = self.gpu_frame()
        return img.rotate(-self.rotate, expand=True) if self.rotate else img

    def next(self, timeout):
        claude = False
        if self.board:
            if self.board.active():
                self.active_at = time.monotonic()
            claude = self.force or time.monotonic() - self.active_at < self.HOLD
        if self.fade:
            period = 1.0 / claude_board.FPS
        elif claude:
            period = 1.0 / claude_board.fps(self.board.snapshot())
        else:
            period = self.period
        time.sleep(max(0.0, self.due - time.monotonic()))
        self.due = max(self.due + period, time.monotonic())
        img = self.claude_frame() if claude else self.gpu_frame()
        view = "claude" if claude else "gpu"
        now = time.monotonic()
        if view != self.view:
            self.fade = (self.last, now) if self.last is not None else None
            self.view = view
        if self.fade:
            k = (now - self.fade[1]) / self.FADE
            if k >= 1 or self.fade[0].size != img.size:
                self.fade = None
            else:
                img = Image.blend(self.fade[0], img, k * k * (3 - 2 * k))   # smoothstep
        self.last = img
        if self.rotate:
            img = img.rotate(-self.rotate, expand=True)
        b = io.BytesIO()
        img.convert("RGB").save(b, "JPEG", quality=self.quality)
        return b.getvalue()

    def close(self):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    ap.add_argument("--fps", type=float, default=2.0, help="dashboard frame rate")
    ap.add_argument("--web-fps", type=float, default=10.0, help="maximum frame rate in web mode")
    ap.add_argument("--video-fps", type=int, default=30, help="maximum frame rate in video mode")
    ap.add_argument("--monitor-fps", type=float, default=30.0, help="maximum frame rate in monitor mode")
    ap.add_argument("--quality", type=int, default=85)
    ap.add_argument("--wireview", nargs="*",
                    default=os.environ.get("WIREVIEWS", "a=/dev/wireview-pro2-a b=/dev/wireview-pro2-b").split(),
                    help="NAME=DEVICE pairs, shown in this order (default: $WIREVIEWS, or units a and b); "
                         "pass with no values to disable")
    ap.add_argument("--control", default=os.path.join(os.environ["RUNTIME_DIRECTORY"], "control.sock")
                    if os.environ.get("RUNTIME_DIRECTORY") else None,
                    help="Unix socket for lightning-screen (mode switching); default: none outside systemd")
    ap.add_argument("--control-users", nargs="*", default=[], metavar="USER",
                    help="users allowed to switch modes (root and the service user always are)")
    ap.add_argument("--preview", help="render one dashboard frame to this PNG and exit (no display needed)")
    a = ap.parse_args()

    W, H = 1280, 800                      # panel size; checked against the device when it connects
    gpu, cpu = Gpu(), Cpu()
    wv = WireViews([tuple(x.split("=", 1)) for x in a.wireview]) if a.wireview else None
    hist = dict(power=collections.deque(maxlen=HIST), temp=collections.deque(maxlen=HIST))
    board = claude_board.Board()
    dash = Dashboard(gpu, cpu, wv, hist, W, H, a.rotate, a.fps, a.quality, board=board)

    if a.preview:
        time.sleep(1.5)  # let CPU load / WireView get a sample
        dash.image().save(a.preview)
        print("wrote", a.preview)
        return

    cache = os.environ.get("CACHE_DIRECTORY")

    def make(req, fd):
        m = req.get("mode")
        if m in ("dashboard", "claude"):
            dash.force = m == "claude"           # claude: keep the sessions board on screen
            return dash
        if m == "web":
            if not str(req.get("url", "")).startswith(("http://", "https://")):
                raise ValueError("web mode needs an http(s) URL")
            prof = os.path.join(cache, "browser") if cache else None
            return modes.WebPage(req["url"], W, H, a.rotate, a.quality, profile_dir=prof)
        if m == "video":
            if req.get("file"):                  # the service's own copy of a startup video
                fd = os.open(os.path.join(os.environ.get("STATE_DIRECTORY", ""), req["file"]), os.O_RDONLY)
                try: return make(dict(req, file=None), fd)
                finally: os.close(fd)            # ffmpeg has its own copy by now
            url = req.get("url") or req.get("page")
            if fd is None and not str(url).startswith(("http://", "https://", "rtsp://", "rtmp://")):
                raise ValueError("video mode needs a file or a network URL")
            if fd is None and not req.get("url"):
                url = modes.resolve(url)         # a remembered page link: get a fresh stream URL
            return modes.Video(url, W, H, a.rotate, a.video_fps, a.quality,
                               loop=bool(req.get("loop")), fill=bool(req.get("fill")), fd=fd)
        if m == "monitor":
            import monitor                       # only needs libevdi when used
            return monitor.Monitor(W, H, a.rotate, a.monitor_fps, a.quality)
        raise ValueError(f"unknown mode {m!r}")

    ctl = control.Controller(make, os.environ.get("STATE_DIRECTORY"),
                             set_brightness=lambda v: hidctl.session(brightness=v),
                             on_claude_status=board.update)
    ctl.restore()
    if a.control:
        ctl.serve(a.control, a.control_users)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit())   # stop cleanly on systemctl stop / shutdown
    stopping = threading.Event()

    def retake_screen():
        if not stopping.is_set():
            try: hidctl.session(usb_frames=True)
            except Exception: pass
    last, sent = None, 0.0
    while True:
        scr = aicast.Screen()
        try:
            scr.open()
            print(f"display connected: {scr.w}x{scr.h}", flush=True)
            try:                                 # leave the built-in animation (after power-on)
                info = hidctl.session(usb_frames=True, brightness=ctl.brightness)
                if ctl.brightness is None and isinstance(info.get("Brightness"), int):
                    ctl.brightness = info["Brightness"]
                # once more in a while, in case the screen was still busy switching to the
                # animation (e.g. the previous service instance stopping just before)
                again = threading.Timer(5, retake_screen)
                again.daemon = True
                again.start()
            except Exception as e:
                print(f"control channel: {e}", flush=True)
            if (scr.w, scr.h) != (W, H):
                print(f"warning: panel is {scr.w}x{scr.h}, frames are {W}x{H}", flush=True)
            if last:
                scr.send_jpeg(last)
            while True:
                mode = ctl.current()
                jpg = mode.next(1.0)
                if jpg:
                    last = jpg
                elif not last or time.monotonic() - sent < 2:
                    continue                     # nothing new; repeat the last frame every 2 s
                t0 = time.monotonic()
                scr.send_jpeg(last)
                sent = time.monotonic()
                if isinstance(mode, modes.WebPage):  # the browser is paced by our frame acks
                    time.sleep(max(0.0, 1.0 / a.web_fps - (sent - t0)))
        except (KeyboardInterrupt, SystemExit):
            stopping.set()
            try:
                hidctl.session(usb_frames=False)  # back to MSI's animation, not a frozen frame
            except Exception:
                pass
            scr.close()
            return
        except Exception as e:  # unplugged, reset, not yet present...
            print(f"display: {e}; retrying in 3 s", flush=True)
            scr.close()
            time.sleep(3)


if __name__ == "__main__":
    main()
