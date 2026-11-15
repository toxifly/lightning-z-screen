#!/usr/bin/env python3
"""Stream a moving test pattern to the screen for N seconds (default 20). Stop the service first."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aicast  # noqa: E402
from PIL import Image, ImageDraw, ImageFont

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20
s = aicast.Screen().open()
print(f"connected {s.w}x{s.h} @ {s.fps} fps")
f = ImageFont.truetype("DejaVuSans-Bold.ttf", 90)
cols = [(255,0,0),(0,255,0),(0,0,255),(255,255,0),(0,255,255),(255,0,255),(255,255,255),(40,40,40)]
t0, n = time.monotonic(), 0
try:
    while time.monotonic() - t0 < secs:
        img = Image.new("RGB", (s.w, s.h)); d = ImageDraw.Draw(img)
        for i, c in enumerate(cols): d.rectangle([i*s.w//8, 0, (i+1)*s.w//8, s.h], fill=c)
        d.rectangle([0, s.h//2-90, s.w, s.h//2+90], fill=(0,0,0))
        d.text((s.w//2, s.h//2), f"Linux {time.monotonic()-t0:4.1f}s", font=f, fill="white", anchor="mm")
        s.send_image(img); n += 1
        time.sleep(1/15)
finally:
    print(f"sent {n} frames"); s.close()
