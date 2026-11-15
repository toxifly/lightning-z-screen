#!/usr/bin/env python3
"""Read-only probe: AiCast "get parameters" vendor request (sends nothing that changes the device)."""
import struct, usb.core, usb.util
dev = usb.core.find(idVendor=0x0DB0, idProduct=0x7881)
if dev is None: raise SystemExit("display not found")
data = bytes(dev.ctrl_transfer(usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE, 0, 0, 0, 256, timeout=3000))
print(len(data), "bytes:", data[:64].hex(" "))
if len(data) >= 16:
    ver, chip, fmt, bus, modes, w, h, fps = struct.unpack_from("<8H", data, 0)
    print(f"ver={ver:#x} chip={chip:#x} fmt={fmt} bus={bus} modes={modes} size={w}x{h} fps={fps}")
