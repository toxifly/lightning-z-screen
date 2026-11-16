"""Desktop monitor mode: the screen becomes a real monitor that the desktop extends onto.

An EVDI virtual display (the kernel module DisplayLink docks use) is plugged in with an EDID for
the panel; the compositor (GNOME, KDE, ...) then drives it like any monitor. Whatever it draws
is grabbed from EVDI, compressed to JPEG and sent to the screen, only when something changed.

Needs the evdi module (evdi-dkms, loaded with initial_device_count=1) and libevdi1.
"""
import ctypes, io, select, threading, time
from PIL import Image


class _Rect(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int) for n in ("x1", "y1", "x2", "y2")]


class _Mode(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("refresh_rate", ctypes.c_int),
                ("bits_per_pixel", ctypes.c_int), ("pixel_format", ctypes.c_uint)]


class _Buffer(ctypes.Structure):
    _fields_ = [("id", ctypes.c_int), ("buffer", ctypes.c_void_p), ("width", ctypes.c_int),
                ("height", ctypes.c_int), ("stride", ctypes.c_int),
                ("rects", ctypes.POINTER(_Rect)), ("rect_count", ctypes.c_int)]


_INT = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)
_MODE = ctypes.CFUNCTYPE(None, _Mode, ctypes.c_void_p)


class _Events(ctypes.Structure):    # evdi_event_context; cursor and DDC/CI handlers stay NULL
    _fields_ = [("dpms", _INT), ("mode_changed", _MODE), ("update_ready", _INT), ("crtc_state", _INT),
                ("cursor_set", ctypes.c_void_p), ("cursor_move", ctypes.c_void_p),
                ("ddcci", ctypes.c_void_p), ("user_data", ctypes.c_void_p)]


_lib = None


def _load():
    global _lib
    if _lib is None:
        try:
            lib = ctypes.CDLL("libevdi.so.1")
        except OSError:
            raise RuntimeError("monitor mode needs evdi-dkms and libevdi1 installed")
        h = ctypes.c_void_p
        for name, args, res in [
                ("evdi_check_device", [ctypes.c_int], ctypes.c_int),
                ("evdi_open", [ctypes.c_int], h),
                ("evdi_connect", [h, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint32], None),
                ("evdi_disconnect", [h], None),
                ("evdi_close", [h], None),
                ("evdi_enable_cursor_events", [h, ctypes.c_bool], None),
                ("evdi_register_buffer", [h, _Buffer], None),
                ("evdi_request_update", [h, ctypes.c_int], ctypes.c_bool),
                ("evdi_grab_pixels", [h, ctypes.POINTER(_Rect), ctypes.POINTER(ctypes.c_int)], None),
                ("evdi_handle_events", [h, ctypes.POINTER(_Events)], None),
                ("evdi_get_event_ready", [h], ctypes.c_int)]:
            f = getattr(lib, name)
            f.argtypes, f.restype = args, res
        _lib = lib
    return _lib


def edid(w, h, hz=60, name=b"Lightning Z"):
    """EDID 1.4 offering one mode, w x h at hz with CVT reduced blanking (9.6" panel)."""
    hb, vb, hfp, hs, vfp, vs = 160, 23, 48, 32, 3, 6
    clock = round((w + hb) * (h + vb) * hz / 10_000)             # 10 kHz units
    mm_w, mm_h = (207, 129) if w >= h else (129, 207)
    dtd = bytes([clock & 0xFF, clock >> 8, w & 0xFF, hb & 0xFF, (w >> 8) << 4 | hb >> 8,
                 h & 0xFF, vb & 0xFF, (h >> 8) << 4 | vb >> 8, hfp, hs, vfp << 4 | vs, 0,
                 mm_w & 0xFF, mm_h & 0xFF, (mm_w >> 8) << 4 | mm_h >> 8, 0, 0, 0x1A])  # +hsync -vsync
    name_desc = bytes([0, 0, 0, 0xFC, 0]) + (name + b"\n").ljust(13, b" ")[:13]
    limits = bytes([0, 0, 0, 0xFD, 0, 24, 61, 20, 80, 10, 1, 0x0A]) + b" " * 6   # 24-61 Hz
    e = (b"\x00\xff\xff\xff\xff\xff\xff\x00"
         + bytes([0x36, 0x69, 0x81, 0x78, 1, 0, 0, 0, 1, 36, 1, 4])   # "MSI" 7881, 2026, EDID 1.4
         + bytes([0x80, mm_w // 10, mm_h // 10, 120, 0x06])            # digital, cm, gamma 2.2, sRGB
         + bytes([0xEE, 0x91, 0xA3, 0x54, 0x4C, 0x99, 0x26, 0x0F, 0x50, 0x54])  # sRGB primaries
         + bytes(3) + b"\x01\x01" * 8
         + dtd + name_desc + limits + bytes([0, 0, 0, 0x10, 0]) + bytes(13) + b"\x00")
    return e + bytes([-sum(e) & 0xFF])


# DRM pixel formats (fourcc) -> Pillow raw mode for the bytes in memory (little-endian)
RAW_MODES = {b"XR24": "BGRX", b"AR24": "BGRX", b"XB24": "RGBX", b"AB24": "RGBX",
             b"RX24": "XBGR", b"RA24": "XBGR", b"BX24": "XRGB", b"BA24": "XRGB"}


class Monitor:
    """A screen mode; ``next()`` returns a JPEG when the desktop changed what's on the monitor."""
    done = False

    def __init__(self, w, h, rotate=0, max_fps=30, quality=85):
        lib = _load()
        lw, lh = (h, w) if rotate in (90, 270) else (w, h)
        dev = next((i for i in range(32) if lib.evdi_check_device(i) == 0), None)    # 0: available
        if dev is None:
            raise RuntimeError("no free EVDI device (is the evdi module loaded with initial_device_count=1?)")
        self.h = lib.evdi_open(dev)
        if not self.h:
            raise RuntimeError(f"cannot open /dev/dri/card{dev}")
        self.lib, self.size, self.rotate, self.quality = lib, (lw, lh), rotate, quality
        self.period, self.due = 1.0 / max_fps, 0.0
        self.pixels = ctypes.create_string_buffer(lw * lh * 4)
        self.rects = (_Rect * 16)()
        self.registered = self.waiting = self.dirty = self.closed = False
        self.blank = False                       # the desktop turned its screens off (DPMS)
        self.raw = "BGRX"                        # XRGB8888 until the desktop sets a mode
        self.lock = threading.Lock()
        self.events = _Events(dpms=_INT(self._dpms), mode_changed=_MODE(self._mode),
                              update_ready=_INT(lambda *_: self._grab()), crtc_state=_INT(lambda *_: None))
        self.fd = lib.evdi_get_event_ready(self.h)
        # The desktop renders (and copies) the monitor at its refresh rate, so offer no more than
        # the frame rate sent to the screen: 30 Hz halves the desktop's work against 60 Hz.
        e = edid(lw, lh, hz=max(24, min(60, round(max_fps))))
        lib.evdi_connect(self.h, e, len(e), lw * lh)                # the monitor appears now
        lib.evdi_enable_cursor_events(self.h, False)                # cursor drawn into the frames

    # -- EVDI callbacks (run inside evdi_handle_events, under self.lock) ------------------------
    def _mode(self, m, _):
        fourcc = m.pixel_format.to_bytes(4, "little")
        self.raw = RAW_MODES.get(fourcc, "BGRX")
        print(f"monitor: {m.width}x{m.height} @ {m.refresh_rate} Hz, {fourcc.decode(errors='replace')}"
              + ("" if fourcc in RAW_MODES else " (unknown format, assuming XR24)"), flush=True)
        if (m.width, m.height) == self.size and not self.registered:
            w, h = self.size
            self.lib.evdi_register_buffer(self.h, _Buffer(1, ctypes.cast(self.pixels, ctypes.c_void_p),
                                                          w, h, w * 4, self.rects, 0))
            self.registered = True

    def _dpms(self, mode, _):
        self.blank = mode != 0
        self.dirty = True                        # black now, or the desktop again

    def _grab(self):
        self.waiting = False
        n = ctypes.c_int(len(self.rects))
        self.lib.evdi_grab_pixels(self.h, self.rects, ctypes.byref(n))
        self.dirty |= n.value > 0

    # -- mode interface ---------------------------------------------------------------------------
    def next(self, timeout):
        end = time.monotonic() + timeout
        time.sleep(max(0.0, min(self.due, end) - time.monotonic()))        # frame-rate cap
        with self.lock:
            while not self.closed and not self.dirty and (left := end - time.monotonic()) > 0:
                if self.registered and not self.waiting:
                    self.waiting = True
                    if self.lib.evdi_request_update(self.h, 1):          # changes already waiting
                        self._grab()
                        if self.dirty:
                            break
                if select.select([self.fd], [], [], min(left, 0.05))[0]:
                    self.lib.evdi_handle_events(self.h, ctypes.byref(self.events))
            if self.closed or not self.dirty:
                return None
            self.dirty = False
            if self.blank:
                img = Image.new("RGB", self.size)
            else:
                img = Image.frombuffer("RGB", self.size, self.pixels, "raw", self.raw, 0, 1)
            if self.rotate:
                img = img.rotate(-self.rotate, expand=True)
            b = io.BytesIO()
            img.save(b, "JPEG", quality=self.quality)
        self.due = time.monotonic() + self.period
        return b.getvalue()

    def close(self):
        with self.lock:                          # not while next() is inside libevdi
            if not self.closed:
                self.closed = True
                self.lib.evdi_disconnect(self.h)     # the monitor disappears
                self.lib.evdi_close(self.h)
