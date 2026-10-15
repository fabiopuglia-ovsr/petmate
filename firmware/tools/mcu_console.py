#!/usr/bin/env python3
"""Petmate MCU console - minimal host for the USB CDC protocol.

Implements docs/INTERFACES.md §2 exactly (same as firmware/src/protocol.*):
  frame  = COBS([type:u8][seq:u8][payload...][crc16:u16 LE]) + 0x00
  crc    = CRC-16/CCITT-FALSE over type|seq|payload
  fields = little-endian, float = IEEE754 binary32

Examples:
  mcu_console.py --selftest                       # offline protocol check
  mcu_console.py /dev/petmate_mcu ping
  mcu_console.py /dev/petmate_mcu state --count 10
  mcu_console.py /dev/petmate_mcu vel 0.15 0.0 --duration 2
  mcu_console.py /dev/petmate_mcu feed 25
  mcu_console.py /dev/petmate_mcu monitor         # print everything received

Requires pyserial (pip install pyserial) except for --selftest.
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from dataclasses import dataclass

# --------------------------------------------------------------- message ids
CMD_VEL, FEED, LAUNCH, INTAKE, TEASE, LED, ESTOP, SET_PARAM, PING, RESET_ODOM, FEED_ABORT, TARE_SCALE = range(1, 13)
STATE, FEED_STATUS, LAUNCH_STATUS, EVENT, PONG, LOG = range(0x81, 0x87)

# Payload layouts (little-endian). Must match protocol.h SIZE constants.
FMT = {
    CMD_VEL: "<hh",
    FEED: "<H",
    LAUNCH: "<BB",
    INTAKE: "<B",
    TEASE: "<BH",
    LED: "<BBBB",
    ESTOP: "<B",
    SET_PARAM: "<Bf",
    PING: "<",
    RESET_ODOM: "<",
    FEED_ABORT: "<",
    TARE_SCALE: "<",
    STATE: "<I5f2i6h3BHhB",
    FEED_STATUS: "<Bff",
    LAUNCH_STATUS: "<BB",
    EVENT: "<BH",
    PONG: "<BBBI",
}
SIZES = {STATE: 52, FEED_STATUS: 9, LAUNCH_STATUS: 2, EVENT: 3, PONG: 7, CMD_VEL: 4, SET_PARAM: 5}
for _t, _n in SIZES.items():
    assert struct.calcsize(FMT[_t]) == _n, (_t, struct.calcsize(FMT[_t]))

STATE_FIELDS = ("t_ms x y th v w enc_l enc_r ax_mg ay_mg az_mg gx_cdps gy_cdps gz_cdps "
                "cliff_mask bump_mask flags batt_mv batt_ma soc_pct").split()
FLAG_NAMES = ["estop", "wheel_drop", "motor_fault", "charging", "docked", "button", "watchdog"]
FEED_STATES = ["idle", "dispensing", "done", "jam", "hopper_empty", "aborted"]
LAUNCH_STATES = ["idle", "spinning_up", "launching", "done", "empty"]
EVENT_NAMES = {1: "cliff", 2: "bump", 3: "wheel_drop", 4: "motor_fault", 5: "button_short",
               6: "button_long", 7: "button_provision", 8: "dock_contact", 9: "undock",
               10: "battery_low", 11: "battery_critical", 12: "feeder_jam", 13: "hopper_low",
               14: "watchdog"}
PARAM_IDS = {"kp": 1, "ki": 2, "kd": 3, "ticks_per_rev": 4, "wheel_radius_m": 5, "track_m": 6,
             "v_max": 7, "w_max": 8, "a_max": 9, "scale_factor": 10, "cliff_threshold_mm": 11}


# --------------------------------------------------------------------- CRC
def crc16_ccitt_false(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# -------------------------------------------------------------------- COBS
def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_idx, code = 0, 1
    for i, b in enumerate(data):
        if b == 0:
            out[code_idx] = code
            code_idx, code = len(out), 1
            out.append(0)
        else:
            out.append(b)
            code += 1
            if code == 0xFF and i + 1 < len(data):  # canonical: no trailing block
                out[code_idx] = code
                code_idx, code = len(out), 1
                out.append(0)
    out[code_idx] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        i += 1
        if code == 0 or i + code - 1 > len(data):
            raise ValueError("malformed COBS")
        block = data[i:i + code - 1]
        if 0 in block:
            raise ValueError("zero inside COBS block")
        out += block
        i += code - 1
        if code != 0xFF and i < len(data):
            out.append(0)
    return bytes(out)


# ------------------------------------------------------------------ frames
def build_frame(msg_type: int, seq: int, payload: bytes = b"") -> bytes:
    body = bytes([msg_type & 0xFF, seq & 0xFF]) + payload
    raw = body + struct.pack("<H", crc16_ccitt_false(body))
    return cobs_encode(raw) + b"\x00"


@dataclass
class Frame:
    type: int
    seq: int
    payload: bytes


class FrameDecoder:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes):
        for b in data:
            if b != 0:
                if len(self.buf) < 512:
                    self.buf.append(b)
                continue
            chunk, self.buf = bytes(self.buf), bytearray()
            if not chunk:
                continue
            try:
                raw = cobs_decode(chunk)
            except ValueError:
                self.errors += 1
                continue
            if len(raw) < 4:
                self.errors += 1
                continue
            body, (crc,) = raw[:-2], struct.unpack("<H", raw[-2:])
            if crc16_ccitt_false(body) != crc:
                self.errors += 1
                continue
            yield Frame(body[0], body[1], body[2:])


def decode_payload(f: Frame):
    if f.type == LOG:
        return f.payload.decode("ascii", "replace")
    fmt = FMT.get(f.type)
    if fmt is None or struct.calcsize(fmt) != len(f.payload):
        return None
    vals = struct.unpack(fmt, f.payload)
    if f.type == STATE:
        return dict(zip(STATE_FIELDS, vals))
    return vals


def fmt_frame(f: Frame) -> str:
    d = decode_payload(f)
    if d is None:
        return f"type=0x{f.type:02x} seq={f.seq} len={len(f.payload)} (undecodable)"
    if f.type == STATE:
        flags = [n for i, n in enumerate(FLAG_NAMES) if d["flags"] & (1 << i)]
        return (f"STATE t={d['t_ms']}ms pose=({d['x']:+.3f},{d['y']:+.3f},{d['th']:+.3f}) "
                f"v={d['v']:+.3f} w={d['w']:+.3f} enc=({d['enc_l']},{d['enc_r']}) "
                f"acc=({d['ax_mg']},{d['ay_mg']},{d['az_mg']})mg gyro=({d['gx_cdps']},{d['gy_cdps']},{d['gz_cdps']})cdps "
                f"cliff=0x{d['cliff_mask']:x} bump=0x{d['bump_mask']:x} flags={flags} "
                f"batt={d['batt_mv']}mV {d['batt_ma']}mA {d['soc_pct']}%")
    if f.type == PONG:
        return f"PONG fw={d[0]}.{d[1]}.{d[2]} uptime={d[3]}ms seq={f.seq}"
    if f.type == FEED_STATUS:
        st = FEED_STATES[d[0]] if d[0] < len(FEED_STATES) else d[0]
        return f"FEED_STATUS state={st} dispensed={d[1]:.1f}g hopper={d[2]:.1f}g"
    if f.type == LAUNCH_STATUS:
        st = LAUNCH_STATES[d[0]] if d[0] < len(LAUNCH_STATES) else d[0]
        return f"LAUNCH_STATUS state={st} balls={d[1]}"
    if f.type == EVENT:
        return f"EVENT {EVENT_NAMES.get(d[0], d[0])} data={d[1]}"
    if f.type == LOG:
        return f"LOG {d}"
    return f"type=0x{f.type:02x} {d}"


# ------------------------------------------------------------------ client
class Mcu:
    def __init__(self, port: str) -> None:
        import serial  # pyserial, imported lazily so --selftest needs no deps
        self.ser = serial.Serial(port, baudrate=115200, timeout=0.01)  # baud ignored (CDC)
        self.dec = FrameDecoder()
        self.seq = 0

    def send(self, msg_type: int, *fields) -> None:
        payload = struct.pack(FMT[msg_type], *fields)
        self.ser.write(build_frame(msg_type, self.seq, payload))
        self.seq = (self.seq + 1) & 0xFF

    def read(self):
        data = self.ser.read(4096)
        return list(self.dec.feed(data)) if data else []


def run(args) -> int:
    mcu = Mcu(args.port)
    t_end = time.monotonic() + args.duration
    states_seen = 0
    last_keepalive = 0.0

    if args.cmd == "ping":
        mcu.send(PING)
    elif args.cmd == "feed":
        mcu.send(FEED, int(round(args.grams * 10)))
    elif args.cmd == "feed_abort":
        mcu.send(FEED_ABORT)
    elif args.cmd == "tare":
        mcu.send(TARE_SCALE)
    elif args.cmd == "launch":
        mcu.send(LAUNCH, args.power, args.count)
    elif args.cmd == "estop":
        mcu.send(ESTOP, 1 if args.engage else 0)
    elif args.cmd == "led":
        mcu.send(LED, args.mode, *args.rgb)
    elif args.cmd == "param":
        pid = PARAM_IDS.get(args.name) or int(args.name)
        mcu.send(SET_PARAM, pid, args.value)
    elif args.cmd == "reset_odom":
        mcu.send(RESET_ODOM)

    try:
        while time.monotonic() < t_end:
            now = time.monotonic()
            # Keep the 250 ms host watchdog fed: CMD_VEL at 20 Hz while driving,
            # PING at 5 Hz otherwise.
            if args.cmd == "vel" and now - last_keepalive >= 0.05:
                mcu.send(CMD_VEL, int(round(args.v * 1000)), int(round(args.w * 1000)))
                last_keepalive = now
            elif args.cmd != "vel" and args.keepalive and now - last_keepalive >= 0.2:
                mcu.send(PING)
                last_keepalive = now
            for f in mcu.read():
                if f.type == STATE:
                    states_seen += 1
                    if args.cmd in ("state", "vel", "monitor") and states_seen % args.decimate == 0:
                        print(fmt_frame(f))
                    if args.cmd == "state" and states_seen >= args.count * args.decimate:
                        return 0
                elif f.type == PONG and args.keepalive and args.cmd not in ("ping", "monitor"):
                    pass  # keepalive replies, not interesting
                else:
                    print(fmt_frame(f))
                    if args.cmd == "ping" and f.type == PONG:
                        return 0
    finally:
        if args.cmd == "vel":
            mcu.send(CMD_VEL, 0, 0)
        if mcu.dec.errors:
            print(f"# {mcu.dec.errors} malformed frames received", file=sys.stderr)
    if args.cmd == "ping":
        print("no PONG received", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------- self-test
def selftest() -> int:
    assert crc16_ccitt_false(b"123456789") == 0x29B1
    vectors = [(b"\x00", b"\x01\x01"), (b"\x00\x00", b"\x01\x01\x01"),
               (b"\x11\x22\x00\x33", b"\x03\x11\x22\x02\x33"), (b"\x11\x22\x33\x44", b"\x05\x11\x22\x33\x44"),
               (b"\x11\x00\x00\x00", b"\x02\x11\x01\x01\x01"), (b"", b"\x01")]
    for raw, enc in vectors:
        assert cobs_encode(raw) == enc, (raw, cobs_encode(raw))
        assert cobs_decode(enc) == raw
    blk = bytes(range(1, 255))
    assert cobs_encode(blk) == b"\xff" + blk
    assert cobs_decode(cobs_encode(blk + b"\x01")) == blk + b"\x01"
    # Golden vectors printed by the C++ unit test (firmware/test).
    assert build_frame(PING, 7).hex() == "05090770d700"
    assert build_frame(CMD_VEL, 1, struct.pack(FMT[CMD_VEL], 200, 0)).hex() == "040101c801010386d700"
    # STATE round trip through the stream decoder, with leading garbage.
    vals = (123456, 1.25, -0.5, 3.0, 0.25, -1.0, -100000, 2000000, -1000, 0, 981, 0, 0, -4500,
            0x0F, 3, 0x7F, 16800, -1200, 99)
    frame = build_frame(STATE, 250, struct.pack(FMT[STATE], *vals))
    dec = FrameDecoder()
    got = list(dec.feed(b"\x13\x37\x00" + frame))
    assert len(got) == 1 and got[0].type == STATE and got[0].seq == 250
    d = decode_payload(got[0])
    assert tuple(d[k] for k in STATE_FIELDS) == vals
    assert dec.errors == 1
    # Corrupted CRC is rejected.
    bad = bytearray(frame)
    bad[5] ^= 0x01
    assert not list(FrameDecoder().feed(bytes(bad)))
    print("selftest OK")
    print(fmt_frame(got[0]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="run offline protocol self-test and exit")
    ap.add_argument("port", nargs="?", default="/dev/petmate_mcu")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ping")
    p = sub.add_parser("state"); p.add_argument("--count", type=int, default=5)
    p = sub.add_parser("vel"); p.add_argument("v", type=float); p.add_argument("w", type=float)
    p = sub.add_parser("feed"); p.add_argument("grams", type=float)
    sub.add_parser("feed_abort")
    sub.add_parser("tare")
    p = sub.add_parser("launch"); p.add_argument("power", type=int); p.add_argument("count", type=int)
    p = sub.add_parser("estop"); p.add_argument("engage", type=int, choices=(0, 1))
    p = sub.add_parser("led"); p.add_argument("mode", type=int); p.add_argument("rgb", type=int, nargs=3)
    p = sub.add_parser("param"); p.add_argument("name"); p.add_argument("value", type=float)
    sub.add_parser("reset_odom")
    sub.add_parser("monitor")
    ap.add_argument("--duration", type=float, default=None, help="seconds to run (default depends on command)")
    ap.add_argument("--decimate", type=int, default=1, help="print every Nth STATE")
    ap.add_argument("--no-keepalive", dest="keepalive", action="store_false",
                    help="do not send PING at 5 Hz (lets the MCU host watchdog trip)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.cmd:
        ap.error("a command is required (or --selftest)")
    if args.duration is None:
        args.duration = {"ping": 1.0, "vel": 2.0, "monitor": 1e9, "feed": 90.0, "launch": 20.0}.get(args.cmd, 3.0)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
