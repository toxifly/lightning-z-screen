#!/usr/bin/env python3
"""MSI Lightning display control channel (HID interface 1), per MSI Lightning Service.

Frame (BYProtocol_x64.dll protocol_format): 0x5A | len16be(n+5) | payload | sum8(len16+payload) | 0x5A,
then bytes between the delimiters are escaped 0x5A->5B 01, 0x5B->5B 02. Sent as one 1024-byte report (ID 0).
Payload is HTTP-like text, e.g. "POST brightness 1\r\nSeqNumber=100\r\nContentType=json\r\nContentLength=N\r\n\r\n{json}".

After power-on the screen plays its built-in animation; `conn` + `extendedDisplay {"enable":true}`
(what MSI's service sends whenever it finds the screen) switch it to the frames sent over USB, and
`{"enable":false}` switches it back. Without that, a warm reboot leaves the last frame frozen.
"""
import glob, json, os, select, sys, threading, time

def find_hidraw():
    for p in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        u = open(p + "/device/uevent").read()
        if "HID_ID=0003:00000DB0:00007881" in u:
            return "/dev/" + os.path.basename(p)
    raise RuntimeError("Lightning display HID interface not found")

def frame(payload: bytes) -> bytes:
    n = len(payload)
    ln = (n + 5).to_bytes(2, "big")
    body = ln + payload + bytes([sum(ln + payload) & 0xFF])
    esc = body.replace(b"\x5b", b"\x5b\x02").replace(b"\x5a", b"\x5b\x01")
    return b"\x5a" + esc + b"\x5a"

def deframe(raw: bytes) -> bytes:
    if not raw or raw[0] != 0x5A: return b""
    end = raw.find(b"\x5a", 1)
    body = raw[1:end].replace(b"\x5b\x01", b"\x5a").replace(b"\x5b\x02", b"\x5b")
    ln = int.from_bytes(body[:2], "big")
    return body[2:2 + ln - 5]

class Ctl:
    def __init__(self, path=None):
        self.fd = os.open(path or find_hidraw(), os.O_RDWR)
    def _write(self, text: str):
        f = frame(text.encode())
        assert len(f) <= 1024, "payload too long"
        os.write(self.fd, b"\x00" + f.ljust(1024, b"\x00"))
        time.sleep(0.01)
    def _read(self, timeout=1.0, want=None):
        """Replies that arrive within timeout; return early once `want` of them have."""
        out = []
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0 and (want is None or len(out) < want):
            if select.select([self.fd], [], [], left)[0]:
                out.append(deframe(os.read(self.fd, 1025)).decode("utf-8", "replace"))
        return out
    def post(self, cmd: str, body=None, seq=100):
        if body is None:
            t = f"POST {cmd} 1\r\nSeqNumber={seq}\r\nContentType=json\r\nDate={int(time.time()*1000)}\r\n\r\n"
        else:
            js = json.dumps(body, separators=(",", ":"))
            t = f"POST {cmd} 1\r\nSeqNumber={seq}\r\nContentType=json\r\nContentLength={len(js)}\r\n\r\n{js}"
        self._write(t)
    def conn(self):
        """Hello; MSI's service sends it twice and waits for the reply before any other command."""
        self.post("conn", seq=1); self.post("conn", seq=1)
        return self._read(1.0, want=1)
    # Like MSI's service, these don't wait for a reply.
    def brightness(self, v): self.post("brightness", {"value": int(v)})
    def rotate(self, deg): self.post("rotate", {"degree": int(deg)})
    def extended(self, on=True): self.post("extendedDisplay", {"enable": on}, seq=110)
    def close(self): os.close(self.fd)

_lock = threading.Lock()

def session(usb_frames=None, brightness=None):
    """One exchange on the control channel: optionally switch between the frames sent over USB
    (True) and the built-in animation (False), and set the brightness (0-100).
    Switching is preceded by `conn`, as MSI's service does; its reply (Brightness, Degree, ...) is
    returned, else {}. Brightness alone skips it: the screen takes it straight away, while the
    `conn` reply often takes over a second."""
    with _lock:
        c = Ctl()
        try:
            info = parse_info(c.conn()) if usb_frames is not None else {}
            if usb_frames is not None: c.extended(usb_frames)
            if brightness is not None: c.brightness(brightness)
            return info
        finally:
            c.close()

def parse_info(replies):
    for r in replies:
        try: return json.loads(r[r.index("{"):r.rindex("}") + 1])
        except ValueError: pass
    return {}

if __name__ == "__main__":
    c = Ctl()
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "conn"), sys.argv[2:]
    actions = {"conn": lambda: None, "extended": c.extended, "animation": lambda: c.extended(False),
               "brightness": lambda: c.brightness(args[0]), "rotate": lambda: c.rotate(args[0])}
    if cmd not in actions or (cmd in ("brightness", "rotate") and not args):
        raise SystemExit("usage: hidctl.py conn|extended|animation|brightness N|rotate DEG")
    replies = c.conn()
    actions[cmd]()
    for x in replies + c._read(0.5): print(repr(x))
