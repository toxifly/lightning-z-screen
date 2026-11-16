"""Extra screen modes next to the GPU dashboard: a live web page and a video.

Every mode exposes ``next(timeout) -> bytes | None`` returning a JPEG that is ready to send
(already at the panel's size and rotation), or None if nothing new arrived in time.
``done`` becomes True when the mode has ended by itself (video finished, browser exited).
"""
import base64, collections, fcntl, io, json, os, queue, re, shutil, subprocess, sys, threading, time
from PIL import Image

BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")


def _rotate_jpeg(jpg, rotate, quality):
    if not rotate:
        return jpg
    b = io.BytesIO()
    Image.open(io.BytesIO(jpg)).rotate(-rotate, expand=True).save(b, "JPEG", quality=quality)
    return b.getvalue()


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()


class _Latest:
    """Holds the newest frame; next() waits for one that hasn't been returned yet."""
    def __init__(self):
        self.cv, self.frame, self.new, self.done = threading.Condition(), None, False, False

    def put(self, f):
        with self.cv:
            self.frame, self.new = f, True
            self.cv.notify()

    def finish(self):
        with self.cv:
            self.done = True
            self.cv.notify()

    def next(self, timeout):
        with self.cv:
            self.cv.wait_for(lambda: self.new or self.done, timeout)
            if not self.new:
                return None
            self.new = False
            return self.frame


class WebPage:
    """A live page rendered by headless Chrome/Chromium, captured with the DevTools screencast.

    DevTools runs over --remote-debugging-pipe (fds 3/4), so no TCP debugging port is opened.
    """
    def __init__(self, url, w, h, rotate=0, quality=85, profile_dir=None):
        exe = next((p for p in map(shutil.which, BROWSERS) if p), None)
        if not exe:
            raise RuntimeError("web mode needs chromium or google-chrome installed")
        self.url, self.rotate, self.quality = url, rotate, quality
        lw, lh = (h, w) if rotate in (90, 270) else (w, h)       # page layout size
        c_in, p_out = os.pipe()     # we write commands -> browser reads on fd 3
        p_in, c_out = os.pipe()     # browser writes on fd 4 -> we read
        args = [exe, "--headless", "--disable-gpu", "--remote-debugging-pipe", "--hide-scrollbars", "--mute-audio",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-background-networking", "--disable-sync", "--force-dark-mode",
                f"--window-size={lw},{lh}", "about:blank"]
        if profile_dir:
            args.insert(1, f"--user-data-dir={profile_dir}")
        def to_fd3_fd4():   # in the child; move both out of the way first so neither clobbers the other
            a, b = fcntl.fcntl(c_in, fcntl.F_DUPFD, 10), fcntl.fcntl(c_out, fcntl.F_DUPFD, 10)
            os.dup2(a, 3); os.dup2(b, 4)
        self.proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(3, 4), preexec_fn=to_fd3_fd4)
        os.close(c_in); os.close(c_out)
        self.wr, self.rd = os.fdopen(p_out, "wb", buffering=0), os.fdopen(p_in, "rb", buffering=0)
        self.lock, self.ids, self.waiting, self.replies = threading.Lock(), 0, set(), {}
        self.reply_cv, self.latest = threading.Condition(), _Latest()
        # The screencast is (re)started on every page load (one started mid-navigation never
        # paints). Headless only streams with everyNthFrame=1, so the rate is limited instead by
        # acknowledging each frame only when the screen takes it (see next()).
        self.cast = dict(format="jpeg", quality=quality, maxWidth=lw, maxHeight=lh, everyNthFrame=1)
        self.sid = self.ack = None
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self._setup(url, lw, lh)
        except Exception:
            self.close()
            raise

    def _setup(self, url, lw, lh):
        # attach to the tab the browser opened (a newly created tab stays hidden and never paints)
        tid = next(t["targetId"] for t in self._call("Target.getTargets")["targetInfos"] if t["type"] == "page")
        self.sid = self._call("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
        self._call("Emulation.setDeviceMetricsOverride", self.sid,
                   width=lw, height=lh, deviceScaleFactor=1, mobile=False)
        self._call("Page.enable", self.sid)
        self._call("Page.navigate", self.sid, url=url)

    @property
    def done(self):
        return self.latest.done

    def _send(self, method, sid=None, wait=False, **params):
        with self.lock:
            self.ids += 1
            if wait:
                self.waiting.add(self.ids)
            msg = {"id": self.ids, "method": method, "params": params}
            if sid: msg["sessionId"] = sid
            self.wr.write(json.dumps(msg).encode() + b"\0")
            return self.ids

    def _call(self, method, sid=None, **params):
        i = self._send(method, sid, wait=True, **params)
        with self.reply_cv:
            if not self.reply_cv.wait_for(lambda: i in self.replies or self.done, 20):
                raise RuntimeError(f"browser did not answer {method}")
            r = self.replies.pop(i, {"error": {"message": "browser exited"}})
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error'].get('message')}")
        return r.get("result", {})

    def _reader(self):
        buf = b""
        try:
            while chunk := self.rd.read(1 << 20):
                buf += chunk
                *msgs, buf = buf.split(b"\0")
                for m in msgs:
                    m = json.loads(m)
                    if m.get("method") == "Page.screencastFrame":
                        self.ack = m["params"]["sessionId"]
                        self.latest.put(base64.b64decode(m["params"]["data"]))
                    elif m.get("method") == "Page.loadEventFired":
                        self._send("Page.startScreencast", self.sid, **self.cast)
                    elif m.get("id") in self.waiting:    # replies nobody waits for are dropped
                        self.waiting.discard(m["id"])
                        with self.reply_cv:
                            self.replies[m["id"]] = m
                            self.reply_cv.notify_all()
        except (OSError, ValueError):
            pass
        self.latest.finish()
        with self.reply_cv:
            self.reply_cv.notify_all()

    def next(self, timeout):
        f = self.latest.next(timeout)
        if not f:
            return None
        self._send("Page.screencastFrameAck", self.sid, sessionId=self.ack)
        return _rotate_jpeg(f, self.rotate, self.quality)

    def close(self):
        _stop(self.proc)
        for f in (self.wr, self.rd):
            try: f.close()
            except OSError: pass


def resolve(page):
    """Get a playable stream URL for a video page (YouTube, ...) with yt-dlp; stream URLs from
    YouTube expire after a few hours, so remembered videos are resolved again at every start."""
    try:
        p = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
                            "-f", "bv*[height<=1080]/b[height<=1080]/b", "-g", page],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("yt-dlp timed out")
    if p.returncode or not p.stdout.strip():
        if "No module named yt_dlp" in p.stderr:
            return page                          # not installed: let ffmpeg try the link itself
        raise RuntimeError(f"yt-dlp: {p.stderr.strip()[-200:]}")
    return p.stdout.split()[0]


class Video:
    """Plays a file, an open file descriptor or a stream URL with ffmpeg.

    ffmpeg scales, letterboxes, rotates and encodes JPEG itself, so Python only forwards frames.
    It decodes ahead into a short buffer, and next() releases frames on an even clock at the
    video's own frame rate (capped at max_fps), so bursty decoding or network reads don't stutter.
    """
    BUFFER = 0.5        # seconds of frames decoded ahead

    def __init__(self, src, w, h, rotate=0, max_fps=30, quality=85, loop=False, fill=False, fd=None):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("video mode needs ffmpeg installed")
        lw, lh = (h, w) if rotate in (90, 270) else (w, h)
        fit = (f"scale={lw}:{lh}:force_original_aspect_ratio=increase,crop={lw}:{lh}" if fill else
               f"scale={lw}:{lh}:force_original_aspect_ratio=decrease,pad={lw}:{lh}:-1:-1:black")
        turn = {0: "", 90: ",transpose=1", 180: ",hflip,vflip", 270: ",transpose=2"}[rotate]
        q = max(2, min(31, round(31 - quality * 0.29)))            # 85 -> ~6
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostats", "-nostdin"]
        if loop: cmd += ["-stream_loop", "-1"]
        cmd += ["-fd", str(fd), "-i", "fd:"] if fd is not None else ["-i", src]
        cmd += ["-an", "-sn", "-vf", f"{fit},format=yuvj420p{turn}", "-fpsmax", str(max_fps),
                "-c:v", "mjpeg", "-q:v", str(q), "-f", "image2pipe", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, pass_fds=(fd,) if fd is not None else ())
        self.max_fps, self.rate, self.log = max_fps, None, collections.deque(maxlen=20)
        self.rate_known = threading.Event()
        self.q = queue.Queue(maxsize=max(2, int(max_fps * self.BUFFER)))
        self.done, self.closed, self.t0, self.n = False, False, None, 0
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _stderr(self):
        for line in self.proc.stderr:
            line = line.decode(errors="replace").rstrip()
            self.log.append(line)
            # the input stream line, e.g. "Stream #0:0: Video: av1 ..., 1590x1080, 24 fps, 24 tbr"
            if not self.rate_known.is_set() and "Video:" in line and (m := re.search(r"([\d.]+) (?:fps|tbr)", line)):
                self.rate = min(float(m.group(1)), self.max_fps)
                self.rate_known.set()
        self.rate_known.set()

    def _reader(self):
        buf = b""
        while chunk := self.proc.stdout.read1(1 << 20):
            buf += chunk
            # ffmpeg's JPEGs contain no thumbnails, so FFD9 only appears as end-of-image
            while (e := buf.find(b"\xff\xd9")) >= 0:
                self._put(buf[buf.find(b"\xff\xd8"):e + 2])   # waits while the buffer is full
                buf = buf[e + 2:]
        self.proc.wait()
        self._put(None)

    def _put(self, item):
        while not self.closed:
            try: return self.q.put(item, timeout=0.5)
            except queue.Full: pass

    def error(self):
        return "\n".join(l for l in self.log if "rror" in l)[-300:]

    def next(self, timeout):
        try:
            f = self.q.get(timeout=timeout)
        except queue.Empty:
            self.t0 = None                       # stalled (network); restart the clock
            return None
        if f is None:
            self.done = True
            return None
        self.rate_known.wait(5)
        period = 1.0 / (self.rate or self.max_fps)
        now = time.monotonic()
        if self.t0 is None or now - (self.t0 + self.n * period) > self.BUFFER:
            self.t0, self.n = now, 0             # first frame, or fell far behind
        time.sleep(max(0.0, self.t0 + self.n * period - now))
        self.n += 1
        return f

    def close(self):
        self.closed = True                       # lets the reader thread finish
        _stop(self.proc)
