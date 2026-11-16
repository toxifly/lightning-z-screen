#!/usr/bin/env python3
"""Read-only WireView Pro II reader.

Sends only two commands, both reads:
  0x04 CMD_READ_SENSOR_VALUES   live per-pin voltage/current, temps, fault bits
  0x0A CMD_SPI_FLASH_READ_PAGE  on-device datalogger (second 8 MB of SPI flash)
Layouts follow emaspa/wireview-linux WireViewDeviceLib and wireview-hwmon.

  wvread.py live [-n COUNT] [-i SECONDS] [--csv FILE]
  wvread.py log  [--csv FILE]        (close the WireView app first)
"""
import argparse, csv, errno, fcntl, os, select, struct, sys, termios, time

CMD_READ_SENSOR_VALUES = 0x04
CMD_SPI_FLASH_READ_PAGE = 0x0A
ALLOWED_CMDS = {CMD_READ_SENSOR_VALUES, CMD_SPI_FLASH_READ_PAGE}

FAULTS = ["chip_over_temp", "sensor_over_temp", "over_current",
          "wire_over_current", "over_power", "current_imbalance"]
LOG_START, LOG_END = 8 * 1024 * 1024, 16 * 1024 * 1024
PAGE, SECTOR, ENTRY = 256, 4096, 21
SENSOR_SIZE = 100


def default_dev():
    return "/dev/wireview-pro2" if os.path.exists("/dev/wireview-pro2") else "/dev/ttyACM0"


def open_port(path, wait_s=5.0):
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            fcntl.ioctl(fd, termios.TIOCEXCL)
            break
        except OSError as e:
            if e.errno != errno.EBUSY or time.monotonic() > deadline:
                raise
            time.sleep(0.05)  # the app holds it for one transaction; retry
    try:
        iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP |
                   termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
        cflag = (cflag & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)) | termios.CS8 | termios.CLOCAL | termios.CREAD
        cc[termios.VMIN], cc[termios.VTIME] = 0, 0
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, termios.B115200, termios.B115200, cc])
        time.sleep(0.1)  # opening makes the device send a welcome string; drop it
        termios.tcflush(fd, termios.TCIOFLUSH)
    except BaseException:
        os.close(fd)     # e.g. unplugged while opening (termios.error)
        raise
    return fd


def close_port(fd):
    try:
        fcntl.ioctl(fd, termios.TIOCNXCL)
    finally:
        os.close(fd)


def transact(fd, payload, size, timeout=1.0):
    if payload[0] not in ALLOWED_CMDS:
        raise ValueError(f"refusing to send command 0x{payload[0]:02X}")
    termios.tcflush(fd, termios.TCIFLUSH)
    os.write(fd, payload)
    buf, deadline = bytearray(), time.monotonic() + timeout
    while len(buf) < size:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return None
        buf += os.read(fd, size - len(buf))
    return bytes(buf)


def parse_sensor(b):
    # Pack=4 layout; reject desynced frames the same way the app does
    if b is None or len(b) != SENSOR_SIZE or b[10] > 100 or b[11] or b[95]:
        return None
    raw_ts = struct.unpack_from("<4h", b, 0)
    raw = [struct.unpack_from("<h2xII", b, 12 + 12 * i) for i in range(6)]
    tp, tc, av, cap, fstat, flog = struct.unpack_from("<IIHBxHH", b, 84)
    # A frame interleaved with another reader (e.g. the WireView app) can pass the
    # checks above; real frames are self-consistent: totals are the per-pin sums,
    # P = V*I per pin, the 12 V rail is present and both onboard sensors are sane.
    if (abs(sum(c for _, c, _ in raw) - tc) > 6 or abs(sum(p for _, _, p in raw) - tp) > 6
            or any(not 10000 <= v <= 14000 or abs(v * c / 1000 - p) > 2 + p / 500 for v, c, p in raw)
            or any(not -200 <= t <= 1500 for t in raw_ts[:2])):
        return None
    ts = [t / 10 for t in raw_ts]
    pins = [(v / 1000, c / 1000, p / 1000) for v, c, p in raw]
    return dict(ts=ts, fan=b[10], pins=pins, total_w=tp / 1000, total_a=tc / 1000,
                avg_v=av / 1000, psu_cap={0: 600, 1: 450, 2: 300, 3: 150}.get(cap, 0),
                fault_status=fstat, fault_log=flog)


def fault_names(mask):
    return ",".join(n for i, n in enumerate(FAULTS) if mask & (1 << i)) or "none"


def cmd_live(a):
    out = None
    if a.csv:
        out = csv.writer(open(a.csv, "w", newline=""))
        out.writerow(["time", "total_w", "total_a", "avg_v"] + [f"I{i}" for i in range(1, 7)] +
                     [f"V{i}" for i in range(1, 7)] + ["t_in", "t_out", "ts1", "ts2", "fan", "fault_status", "fault_log"])
    peak = dict(w=0.0, pin=0.0, pin_idx=0, t=0.0, spread=0.0)
    n = 0
    while a.count == 0 or n < a.count:
        fd = open_port(a.dev)  # open/close per sample so the app can share the port
        try:
            s = parse_sensor(transact(fd, bytes([CMD_READ_SENSOR_VALUES]), SENSOR_SIZE))
        finally:
            close_port(fd)
        if s:
            n += 1
            cur = [p[1] for p in s["pins"]]
            hi = max(cur)
            if s["total_w"] > peak["w"]:
                peak["w"] = s["total_w"]
            if hi > peak["pin"]:
                peak["pin"], peak["pin_idx"] = hi, cur.index(hi) + 1
            peak["t"] = max(peak["t"], max(s["ts"][:2]))
            if s["total_a"] >= 20:
                peak["spread"] = max(peak["spread"], hi / (s["total_a"] / 6))
            line = (f"{time.strftime('%H:%M:%S')} {s['total_w']:7.1f} W {s['total_a']:6.2f} A  pins A: "
                    + " ".join(f"{c:5.2f}" for c in cur)
                    + f"  in/out {s['ts'][0]:.1f}/{s['ts'][1]:.1f} C  fault now={fault_names(s['fault_status'])} log={fault_names(s['fault_log'])}")
            if not a.quiet:
                print(line, flush=True)
            if out:
                out.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), s["total_w"], s["total_a"], s["avg_v"], *cur,
                              *[p[0] for p in s["pins"]], *s["ts"], s["fan"], s["fault_status"], s["fault_log"]])
        time.sleep(a.interval)
    print(f"\nsamples={n} peak_total={peak['w']:.1f} W  peak_pin={peak['pin']:.2f} A (pin {peak['pin_idx']})"
          f"  peak_onboard_temp={peak['t']:.1f} C  worst_pin_vs_avg(>=20A)={peak['spread']:.2f}x")


def parse_log(data):
    """Port of DeviceLogParser.Parse (wireview-linux), same skips and gates."""
    res, first, empty = [], False, 0
    nsec = len(data) // SECTOR
    for sector in range(nsec):
        base, idx = sector * SECTOR, 0
        while idx < SECTOR // ENTRY:
            off = base + idx * ENTRY
            if off + ENTRY > nsec * SECTOR:
                break
            if first and (off & (PAGE - 1)) > PAGE - ENTRY:
                off += PAGE - (off & (PAGE - 1))
                if off % ENTRY:
                    off += ENTRY - off % ENTRY
                idx = max((off - base) // ENTRY, idx + 1)
                continue
            raw = struct.unpack_from("<I", data, off)[0]
            if raw == 0xFFFFFFFF:
                if first:
                    empty += 1
                    if empty >= 32:
                        return res
                idx += 1
                continue
            empty = 0
            typ, stamp = raw & 3, (raw >> 2) & 0x3FFFFFFF
            ts, v, c, hp = data[off + 4:off + 8], data[off + 8:off + 14], data[off + 14:off + 20], data[off + 20]
            if typ == 2:
                res.append(dict(type="power_on", ms=stamp))
                first = True
            elif typ == 0 and hp <= 3 and 60 < sum(v) < 900:
                res.append(dict(type="sample", ms=stamp, ts=list(ts), v=[x / 10 for x in v],
                                i=[x / 10 for x in c], hp=hp))
                first = True
            idx += 1
    return res


def cmd_log(a):
    fd = open_port(a.dev)
    data, t0 = bytearray(), time.monotonic()
    try:
        addr = LOG_START
        while addr < LOG_END:
            chunk = None
            for _ in range(10):
                chunk = transact(fd, bytes([CMD_SPI_FLASH_READ_PAGE]) + struct.pack("<II", addr, PAGE), PAGE)
                if chunk:
                    break
                time.sleep(0.01)
                termios.tcflush(fd, termios.TCIFLUSH)
            if not chunk:
                sys.exit(f"read failed at 0x{addr:X}")
            data += chunk
            addr += PAGE
            if (addr - LOG_START) % (512 * 1024) == 0:
                print(f"  read {(addr - LOG_START) >> 20} / 8 MB ({time.monotonic() - t0:.0f}s)", file=sys.stderr, flush=True)
    finally:
        close_port(fd)

    entries = parse_log(bytes(data))
    cycles, cur = [], None
    for e in entries:
        if e["type"] == "power_on" or cur is None:
            cur = []
            cycles.append(cur)
        if e["type"] == "sample":
            cur.append(e)
    if a.csv:
        w = csv.writer(open(a.csv, "w", newline=""))
        w.writerow(["cycle", "seconds", "total_w"] + [f"I{i}" for i in range(1, 7)] + [f"V{i}" for i in range(1, 7)]
                   + ["t_in", "t_out", "ts1", "ts2"])
        for ci, cyc in enumerate(cycles, 1):
            for e in cyc:
                w.writerow([ci, e["ms"] / 1000, round(sum(v * i for v, i in zip(e["v"], e["i"])), 1), *e["i"], *e["v"], *e["ts"]])
    print(f"{len(entries)} entries, {len(cycles)} power cycles")
    for ci, cyc in enumerate(cycles, 1):
        if not cyc:
            continue
        pw = [sum(v * i for v, i in zip(e["v"], e["i"])) for e in cyc]
        hi_pin = max(cyc, key=lambda e: max(e["i"]))
        loaded = [e for e in cyc if sum(e["i"]) >= 20]
        spread = max((max(e["i"]) / (sum(e["i"]) / 6) for e in loaded), default=0)
        print(f"cycle {ci:3d}: {len(cyc):6d} samples over {cyc[-1]['ms'] / 3.6e6:6.2f} h | peak {max(pw):6.1f} W | "
              f"peak pin {max(hi_pin['i']):5.1f} A (pin {hi_pin['i'].index(max(hi_pin['i'])) + 1}) | "
              f"max temp {max(max(e['ts'][:2]) for e in cyc):3d} C | samples >=20 A: {len(loaded):5d} | "
              f"worst pin vs avg there: {spread:.2f}x")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dev", default=default_dev())
    sub = p.add_subparsers(dest="mode", required=True)
    l = sub.add_parser("live")
    l.add_argument("-n", "--count", type=int, default=5, help="samples, 0 = until Ctrl+C")
    l.add_argument("-i", "--interval", type=float, default=1.0)
    l.add_argument("--csv")
    l.add_argument("-q", "--quiet", action="store_true")
    g = sub.add_parser("log")
    g.add_argument("--csv")
    a = p.parse_args()
    try:
        cmd_live(a) if a.mode == "live" else cmd_log(a)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
