"""Mode switching for the screen service over a local Unix socket.

Protocol: the client sends one JSON line and gets one JSON line back.
  {"mode": "dashboard"}
  {"mode": "web", "url": "https://..."}
  {"mode": "monitor"}                        (the desktop extends onto the screen)
  {"mode": "video", "url": "...", "page": "...", "name": "...", "loop": false, "fill": false, "startup": false}
      (or an open file passed with SCM_RIGHTS instead of url/page)
  {"mode": "status"}
  {"mode": "brightness", "value": 0-100}     (without "value": just report it)
The dashboard, a web page, the desktop monitor or a "startup" video (which loops) is remembered
across restarts; a temporary video returns to it when it ends.
Only root, the service user and the users given with --control-users may connect.
"""
import json, os, pwd, shutil, socket, struct, threading, time

STATE_FILE = "mode.json"
BRIGHTNESS_FILE = "brightness"

DASHBOARD = {"mode": "dashboard"}
STARTUP_VIDEO = "startup-video"     # the service's own copy of a startup video file
RETRY = 60                          # seconds before retrying a remembered mode that failed


class Controller:
    """Owns the current mode. The remembered mode (dashboard, web page or startup video) is
    restored at start, after a temporary video ends, and retried if it fails."""

    def __init__(self, make, state_dir=None, set_brightness=None):
        """make(request, fd) -> mode object; raises on bad requests.
        set_brightness(value) applies a brightness to the screen."""
        self.make, self.state_dir, self.set_brightness = make, state_dir, set_brightness
        self.lock = threading.RLock()           # held for a whole switch (make() can take seconds)
        self.req, self.mode, self.since, self.retry_at = DASHBOARD, None, 0.0, None
        self.saved = self._load()
        self.brightness = self._load_brightness()  # None: never set here, leave the screen's own

    # -- persistence --------------------------------------------------------------------------
    def _path(self, name):
        if not self.state_dir:
            raise RuntimeError("no state directory (run as the service to remember modes)")
        return os.path.join(self.state_dir, name)

    def _load(self):
        try:
            with open(self._path(STATE_FILE)) as f:
                return json.load(f)
        except (RuntimeError, OSError, ValueError):
            return DASHBOARD

    def _load_brightness(self):
        try:
            with open(self._path(BRIGHTNESS_FILE)) as f:
                return int(f.read())
        except (RuntimeError, OSError, ValueError):
            return None

    def brighten(self, value):
        value = max(0, min(100, int(value)))
        if not self.set_brightness:
            raise RuntimeError("brightness control is not available")
        self.set_brightness(value)
        self.brightness = value
        if self.state_dir:
            with open(self._path(BRIGHTNESS_FILE), "w") as f:
                f.write(str(value))

    def _remember(self, req):
        self.saved = {k: v for k, v in req.items() if k != "url" or req["mode"] == "web"}
        if not self.state_dir:
            return
        tmp = self._path(STATE_FILE + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self.saved, f)
        os.replace(tmp, self._path(STATE_FILE))
        if "file" not in self.saved:
            try: os.unlink(self._path(STARTUP_VIDEO))
            except FileNotFoundError: pass

    def _keep_copy(self, fd):
        """Copy a video handed over as an open file, so it can still play after a restart."""
        tmp = self._path(STARTUP_VIDEO + ".tmp")
        with open(os.dup(fd), "rb") as src, open(tmp, "wb") as dst:
            src.seek(0)
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp, self._path(STARTUP_VIDEO))

    # -- switching ----------------------------------------------------------------------------
    def switch(self, req, fd=None, remember=True):
        if req.get("startup"):
            req = dict(req, loop=True)
            if fd is not None:
                self._keep_copy(fd)
                req, fd = dict(req, file=STARTUP_VIDEO), None
        with self.lock:
            new = self.make(req, fd)            # may take a few seconds (browser start-up, yt-dlp)
            old, self.mode, self.req = self.mode, new, req
            self.since, self.retry_at = time.monotonic(), None
            if old is not None and old is not new:
                old.close()
            if remember and (req["mode"] in ("dashboard", "web", "monitor") or req.get("startup")):
                self._remember(req)
        print(f"mode: {describe(req)}", flush=True)

    def _fall_back(self):
        self.switch(DASHBOARD, remember=False)
        self.retry_at = time.monotonic() + RETRY

    def restore(self):
        """Enter the remembered mode; if it fails, show the dashboard and retry later."""
        try:
            self.switch(self.saved, remember=False)
        except Exception as e:
            print(f"mode: cannot show {describe(self.saved)}: {e}; retrying in {RETRY} s", flush=True)
            self._fall_back()

    def current(self):
        """The mode to draw from; called by the frame loop, which must never wait on a switch."""
        m = self.mode
        due = m.done or (self.retry_at and time.monotonic() >= self.retry_at)
        if due and self.lock.acquire(blocking=False):   # busy: a client is switching right now
            try:
                if self.mode is m:
                    self._end_or_retry(m)
            finally:
                self.lock.release()
        return self.mode

    def _end_or_retry(self, m):
        if not m.done:
            return self.restore()
        err = getattr(m, "error", lambda: "")()     # video finished, stream expired or browser died
        print(f"mode: {describe(self.req)} ended{': ' + err if err else ''}", flush=True)
        if self.req == self.saved and time.monotonic() - self.since < 10:
            self._fall_back()                       # failing straight away: don't spin
        else:
            self.restore()

    # -- socket server ------------------------------------------------------------------------
    def serve(self, path, users=()):
        allowed = {0, os.getuid()} | {pwd.getpwnam(u).pw_uid for u in users}
        try: os.unlink(path)
        except FileNotFoundError: pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o666)                   # access is checked per connection (SO_PEERCRED)
        srv.listen(4)

        def loop():
            while True:
                conn, _ = srv.accept()
                with conn:
                    self._handle(conn, allowed)
        threading.Thread(target=loop, daemon=True).start()

    def _handle(self, conn, allowed):
        fds = []
        try:
            conn.settimeout(30)
            _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in allowed:
                return reply(conn, ok=False, error="not allowed (see --control-users)")
            data, fds, _, _ = socket.recv_fds(conn, 65536, 1)
            while not data.endswith(b"\n") and (more := conn.recv(65536)):
                data += more
            req = json.loads(data)
            if req.get("mode") == "status":
                return reply(conn, ok=True, **self.req)
            if req.get("mode") == "brightness":
                if "value" in req:
                    self.brighten(req["value"])
                return reply(conn, ok=True, value=self.brightness)
            self.switch(req, fds[0] if fds else None)
            reply(conn, ok=True, **req)
        except Exception as e:
            reply(conn, ok=False, error=str(e))
        finally:
            for fd in fds:                      # the mode has handed it to ffmpeg by now
                os.close(fd)


def reply(conn, **r):
    try: conn.sendall(json.dumps(r).encode() + b"\n")
    except OSError: pass


def describe(r):
    what = r.get("name") or r.get("url")
    return r["mode"] + (f" {what}" if what else "")
