#!/usr/bin/env python3
"""Minimal AiCast (ArtInChip USB display) client for the MSI RTX 5090 Lightning Z screen.

Protocol per hevnsnt/artinchip-linux (MIT), reverse-engineered from ArtInChip's AiCast driver:
vendor ctrl IN req 0 -> params; RSA two-phase auth on bulk EP1; then JPEG frames
with a 20-byte header (FRAME_START_MAGIC, len, frame_id, media_format, 0, magic).
"""
import io, os, secrets, struct, time
import usb.core, usb.util
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

FRAME_MAGIC, AUTH_DEV, AUTH_HOST = 0xA1C62B01, 0xA1C62B10, 0xA1C62B11
EP_OUT, EP_IN, CHUNK = 0x01, 0x81, 4096 * 64
IDS = [(0x0DB0, 0x7881)]          # MSI V530-9.6 Lightning
PUB = serialization.load_pem_public_key(b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAybdtvB1uNA4XICh+xJi1
KJWO0GYal4lNiW69zSMIJFGzb2wkiFBX2txFaH5ZYh0TYdwmjzBqinzTsWhIasW3
rl9QN5cv73zFalO3J4hADXz1g7hlHVB0BKDD280NUKUGAbwDv+KMHTprs+B/T4QU
a0s4RBNnN4fMPk2H0UAWU1jKAvMYjh/YR+MLYbl04ZCLlOfX9zQjRBVan7aLARQg
v5QRahAlAoBsYK864VrBKq91lRCXt4XP5d/sDtZM7kGcpLi2i4xHtRct37M+bkZv
Lf/3aVpAVsqZy5P2NXEe6HMv4Q+YP6QKz2wuk3xWYHWFn+88ydjv394tN28rjl56
hwIDAQAB
-----END PUBLIC KEY-----""")


def _hdr(magic, length, fid=0, fmt=0):
    return struct.pack("<IIHHII", magic, length, fid & 0xFFFF, fmt, 0, magic)


class Screen:
    def __init__(self):
        self.dev = None
        self.w = self.h = self.fmt = self.fps = 0
        self.fid = 0

    def open(self):
        for vid, pid in IDS:
            self.dev = usb.core.find(idVendor=vid, idProduct=pid)
            if self.dev: break
        if not self.dev:
            raise RuntimeError("Lightning display not found (is the USB-C cable connected?)")
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        usb.util.claim_interface(self.dev, 0)   # bulk interface only; HID (if 1) is left alone
        p = bytes(self.dev.ctrl_transfer(0xC0, 0, 0, 0, 256, timeout=3000))
        _, _, self.fmt, _, _, self.w, self.h, self.fps = struct.unpack_from("<8H", p, 0)
        self._auth()
        return self

    def _auth(self):
        n, e = PUB.public_numbers().n, PUB.public_numbers().e
        ch = os.urandom(secrets.randbelow(244) + 1)
        self.dev.write(EP_OUT, _hdr(AUTH_DEV, 0x100), timeout=3000)
        self.dev.write(EP_OUT, PUB.encrypt(ch, padding.PKCS1v15()), timeout=3000)
        r = bytes(self.dev.read(EP_IN, 256, timeout=3000))
        if r[:len(ch)] != ch:
            raise RuntimeError("display failed device authentication")
        self.dev.write(EP_OUT, _hdr(AUTH_HOST, 0x100), timeout=3000)
        s = bytes(self.dev.read(EP_IN, 256, timeout=3000))
        m = pow(int.from_bytes(s, "big"), e, n).to_bytes((n.bit_length() + 7) // 8, "big")
        if m[:2] != b"\x00\x01":
            raise RuntimeError("bad signature padding from display")
        i = 2
        while m[i] == 0xFF: i += 1
        self.dev.write(EP_OUT, m[i + 1:], timeout=3000)

    def send_jpeg(self, jpg: bytes):
        self.dev.write(EP_OUT, _hdr(FRAME_MAGIC, len(jpg), self.fid, self.fmt), timeout=5000)
        for o in range(0, len(jpg), CHUNK):
            self.dev.write(EP_OUT, jpg[o:o + CHUNK], timeout=10000)
        self.fid += 1

    def send_image(self, img, quality=85):
        b = io.BytesIO()
        img.convert("RGB").save(b, "JPEG", quality=quality)
        self.send_jpeg(b.getvalue())

    def close(self):
        if self.dev:
            try: usb.util.release_interface(self.dev, 0)
            except Exception: pass
            usb.util.dispose_resources(self.dev)
            self.dev = None


if __name__ == "__main__":
    from PIL import Image, ImageDraw, ImageFont
    s = Screen().open()
    print(f"connected: {s.w}x{s.h} fmt={s.fmt} fps={s.fps}")
    img = Image.new("RGB", (s.w, s.h))
    d = ImageDraw.Draw(img)
    cols = [(255,0,0),(0,255,0),(0,0,255),(255,255,0),(0,255,255),(255,0,255),(255,255,255),(40,40,40)]
    for i, c in enumerate(cols):
        d.rectangle([i * s.w // 8, 0, (i + 1) * s.w // 8, s.h], fill=c)
    try: f = ImageFont.truetype("DejaVuSans-Bold.ttf", 72)
    except OSError: f = ImageFont.load_default()
    d.rectangle([0, s.h//2 - 70, s.w, s.h//2 + 70], fill=(0, 0, 0))
    d.text((s.w//2, s.h//2), "Hello from Linux", font=f, fill="white", anchor="mm")
    for _ in range(3):
        s.send_image(img); time.sleep(0.2)
    print("test frame sent")
    s.close()
