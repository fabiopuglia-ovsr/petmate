"""Pure-Python codec for the Petmate host <-> MCU serial protocol.

INTERFACES.md §2: COBS framing (delimiter 0x00); decoded frame is
``[type:u8][seq:u8][payload...][crc:u16 LE]`` with CRC-16/CCITT-FALSE over
``type|seq|payload``. All fields little-endian. No ROS dependency here so it
can be unit tested on any machine.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Iterator, Tuple

# ---------------------------------------------------------------- types
CMD_VEL = 0x01
FEED = 0x02
LAUNCH = 0x03
INTAKE = 0x04
TEASE = 0x05
LED = 0x06
ESTOP = 0x07
SET_PARAM = 0x08
PING = 0x09
RESET_ODOM = 0x0A
FEED_ABORT = 0x0B
TARE_SCALE = 0x0C

STATE = 0x81
FEED_STATUS = 0x82
LAUNCH_STATUS = 0x83
EVENT = 0x84
PONG = 0x85
LOG = 0x86

# SET_PARAM ids
PARAM_KP = 1
PARAM_KI = 2
PARAM_KD = 3
PARAM_TICKS_PER_REV = 4
PARAM_WHEEL_RADIUS_M = 5
PARAM_TRACK_M = 6
PARAM_V_MAX = 7
PARAM_W_MAX = 8
PARAM_A_MAX = 9
PARAM_SCALE_FACTOR = 10
PARAM_CLIFF_THRESHOLD_MM = 11

INTAKE_MODES = {'off': 0, 'in': 1, 'out': 2}
TEASE_PATTERNS = {'stop': 0, 'wiggle': 1, 'sweep': 2, 'random': 3}
LED_MODES = {'off': 0, 'solid': 1, 'breathe': 2, 'rainbow': 3, 'chase': 4}

FEED_STATES = {0: 'idle', 1: 'dispensing', 2: 'done', 3: 'jam',
               4: 'hopper_empty', 5: 'aborted'}
LAUNCH_STATES = {0: 'idle', 1: 'spinning_up', 2: 'launching', 3: 'done',
                 4: 'empty'}
EVENT_CODES = {1: 'cliff', 2: 'bump', 3: 'wheel_drop', 4: 'motor_fault',
               5: 'button_short', 6: 'button_long', 7: 'button_provision',
               8: 'dock_contact', 9: 'undock', 10: 'battery_low',
               11: 'battery_critical', 12: 'feeder_jam', 13: 'hopper_low',
               14: 'watchdog'}

FLAG_ESTOP = 1 << 0
FLAG_WHEEL_DROP = 1 << 1
FLAG_MOTOR_FAULT = 1 << 2
FLAG_CHARGING = 1 << 3
FLAG_DOCKED = 1 << 4
FLAG_BUTTON = 1 << 5
FLAG_WATCHDOG = 1 << 6

MAX_FRAME = 512

# ---------------------------------------------------------------- CRC / COBS


def crc16_ccitt_false(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, xorout 0."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    """COBS-encode ``data`` (output contains no 0x00; delimiter not appended)."""
    out = bytearray()
    code_idx = 0
    out.append(0)
    code = 1
    for b in data:
        if b == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_idx] = code
                code_idx = len(out)
                out.append(0)
                code = 1
    out[code_idx] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    """Decode one COBS block (without the trailing 0x00). Raises ValueError."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise ValueError('zero byte inside COBS block')
        i += 1
        end = i + code - 1
        if end > n:
            raise ValueError('truncated COBS block')
        chunk = data[i:end]
        if 0 in chunk:
            raise ValueError('zero byte inside COBS block')
        out += chunk
        i = end
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


# ---------------------------------------------------------------- framing


def build_frame(ptype: int, seq: int, payload: bytes = b'') -> bytes:
    """Return the on-wire bytes (COBS + 0x00 delimiter) for one packet."""
    body = bytes([ptype & 0xFF, seq & 0xFF]) + payload
    crc = crc16_ccitt_false(body)
    return cobs_encode(body + struct.pack('<H', crc)) + b'\x00'


def parse_frame(block: bytes) -> Tuple[int, int, bytes]:
    """Parse one COBS block (delimiter stripped). Returns (type, seq, payload)."""
    raw = cobs_decode(block)
    if len(raw) < 4:
        raise ValueError('frame too short')
    body, crc_bytes = raw[:-2], raw[-2:]
    (crc_rx,) = struct.unpack('<H', crc_bytes)
    crc_calc = crc16_ccitt_false(body)
    if crc_rx != crc_calc:
        raise ValueError(f'CRC mismatch rx=0x{crc_rx:04X} calc=0x{crc_calc:04X}')
    return body[0], body[1], bytes(body[2:])


class FrameReader:
    """Incremental stream splitter: feed() bytes, yields (type, seq, payload).

    Bad frames are counted in ``errors`` and dropped (resync on next 0x00).
    """

    def __init__(self, max_frame: int = MAX_FRAME):
        self._buf = bytearray()
        self._max = max_frame
        self.errors = 0
        self.frames = 0

    def feed(self, data: bytes) -> Iterator[Tuple[int, int, bytes]]:
        for b in data:
            if b == 0:
                if self._buf:
                    block = bytes(self._buf)
                    self._buf.clear()
                    try:
                        res = parse_frame(block)
                    except ValueError:
                        self.errors += 1
                        continue
                    self.frames += 1
                    yield res
            else:
                if len(self._buf) >= self._max:
                    self._buf.clear()
                    self.errors += 1
                self._buf.append(b)


class SeqCounter:
    def __init__(self):
        self._s = 0

    def next(self) -> int:
        s = self._s
        self._s = (self._s + 1) & 0xFF
        return s


# ---------------------------------------------------------------- helpers


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _i16(v: float) -> int:
    return int(_clamp(int(round(v)), -32768, 32767))


# ---------------------------------------------------------------- host -> MCU payloads


def enc_cmd_vel(v_m_s: float, w_rad_s: float) -> bytes:
    if not (math.isfinite(v_m_s) and math.isfinite(w_rad_s)):
        v_m_s, w_rad_s = 0.0, 0.0
    return struct.pack('<hh', _i16(v_m_s * 1000.0), _i16(w_rad_s * 1000.0))


def enc_feed(grams: float) -> bytes:
    gx10 = int(_clamp(int(round(grams * 10.0)), 0, 0xFFFF))
    return struct.pack('<H', gx10)


def enc_launch(power_pct: int, count: int) -> bytes:
    return struct.pack('<BB', int(_clamp(int(power_pct), 20, 70)),
                       int(_clamp(int(count), 1, 4)))


def enc_intake(mode: int) -> bytes:
    if mode not in (0, 1, 2):
        raise ValueError('intake mode must be 0, 1 or 2')
    return struct.pack('<B', mode)


def enc_tease(pattern: int, duration_ms: int) -> bytes:
    if pattern not in (0, 1, 2, 3):
        raise ValueError('tease pattern must be 0..3')
    return struct.pack('<BH', pattern, int(_clamp(int(duration_ms), 0, 0xFFFF)))


def enc_led(mode: int, r: int, g: int, b: int) -> bytes:
    if mode not in (0, 1, 2, 3, 4):
        raise ValueError('led mode must be 0..4')
    return struct.pack('<BBBB', mode, r & 0xFF, g & 0xFF, b & 0xFF)


def enc_estop(engage: bool) -> bytes:
    return struct.pack('<B', 1 if engage else 0)


def enc_set_param(param_id: int, value: float) -> bytes:
    return struct.pack('<Bf', param_id & 0xFF, float(value))


def parse_hex_color(color: str) -> Tuple[int, int, int]:
    s = (color or '').strip().lstrip('#')
    if len(s) == 3:
        s = ''.join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f'bad color {color!r}')
    v = int(s, 16)
    return (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF


# ---------------------------------------------------------------- MCU -> host payloads

STATE_FMT = '<I5f2i6h3BHhB'
STATE_SIZE = struct.calcsize(STATE_FMT)  # 52 bytes


@dataclass
class State:
    t_ms: int
    x: float
    y: float
    th: float
    v: float
    w: float
    enc_l: int
    enc_r: int
    ax_mg: int
    ay_mg: int
    az_mg: int
    gx_cdps: int
    gy_cdps: int
    gz_cdps: int
    cliff_mask: int
    bump_mask: int
    flags: int
    batt_mv: int
    batt_ma: int
    soc_pct: int

    @property
    def estop(self) -> bool:
        return bool(self.flags & FLAG_ESTOP)

    @property
    def wheel_drop(self) -> bool:
        return bool(self.flags & FLAG_WHEEL_DROP)

    @property
    def motor_fault(self) -> bool:
        return bool(self.flags & FLAG_MOTOR_FAULT)

    @property
    def charging(self) -> bool:
        return bool(self.flags & FLAG_CHARGING)

    @property
    def docked(self) -> bool:
        return bool(self.flags & FLAG_DOCKED)

    @property
    def button_pressed(self) -> bool:
        return bool(self.flags & FLAG_BUTTON)

    @property
    def watchdog_tripped(self) -> bool:
        return bool(self.flags & FLAG_WATCHDOG)

    # SI conversions
    def accel_m_s2(self) -> Tuple[float, float, float]:
        k = 9.80665 / 1000.0
        return self.ax_mg * k, self.ay_mg * k, self.az_mg * k

    def gyro_rad_s(self) -> Tuple[float, float, float]:
        k = math.pi / 180.0 / 100.0
        return self.gx_cdps * k, self.gy_cdps * k, self.gz_cdps * k


def enc_state(s: State) -> bytes:
    """Encode a STATE payload (used by tests / MCU simulator)."""
    return struct.pack(STATE_FMT, s.t_ms, s.x, s.y, s.th, s.v, s.w, s.enc_l,
                       s.enc_r, s.ax_mg, s.ay_mg, s.az_mg, s.gx_cdps, s.gy_cdps,
                       s.gz_cdps, s.cliff_mask, s.bump_mask, s.flags, s.batt_mv,
                       s.batt_ma, s.soc_pct)


def dec_state(p: bytes) -> State:
    if len(p) != STATE_SIZE:
        raise ValueError(f'STATE payload {len(p)} B, expected {STATE_SIZE}')
    return State(*struct.unpack(STATE_FMT, p))


@dataclass
class FeedStatus:
    state: int
    dispensed_g: float
    hopper_g: float

    @property
    def state_name(self) -> str:
        return FEED_STATES.get(self.state, f'unknown_{self.state}')


def dec_feed_status(p: bytes) -> FeedStatus:
    if len(p) != 9:
        raise ValueError('FEED_STATUS payload must be 9 B')
    return FeedStatus(*struct.unpack('<Bff', p))


@dataclass
class LaunchStatus:
    state: int
    balls: int

    @property
    def state_name(self) -> str:
        return LAUNCH_STATES.get(self.state, f'unknown_{self.state}')


def dec_launch_status(p: bytes) -> LaunchStatus:
    if len(p) != 2:
        raise ValueError('LAUNCH_STATUS payload must be 2 B')
    return LaunchStatus(*struct.unpack('<BB', p))


@dataclass
class Event:
    code: int
    data: int

    @property
    def name(self) -> str:
        return EVENT_CODES.get(self.code, f'unknown_{self.code}')


def dec_event(p: bytes) -> Event:
    if len(p) != 3:
        raise ValueError('EVENT payload must be 3 B')
    return Event(*struct.unpack('<BH', p))


@dataclass
class Pong:
    fw_major: int
    fw_minor: int
    fw_patch: int
    uptime_ms: int

    @property
    def fw(self) -> str:
        return f'{self.fw_major}.{self.fw_minor}.{self.fw_patch}'


def dec_pong(p: bytes) -> Pong:
    if len(p) != 7:
        raise ValueError('PONG payload must be 7 B')
    return Pong(*struct.unpack('<BBBI', p))


def dec_log(p: bytes) -> str:
    return p[:120].decode('ascii', errors='replace')


DECODERS = {
    STATE: dec_state,
    FEED_STATUS: dec_feed_status,
    LAUNCH_STATUS: dec_launch_status,
    EVENT: dec_event,
    PONG: dec_pong,
    LOG: dec_log,
}


def decode_payload(ptype: int, payload: bytes):
    dec = DECODERS.get(ptype)
    if dec is None:
        raise ValueError(f'unknown packet type 0x{ptype:02X}')
    return dec(payload)


def yaw_to_quat(th: float) -> Tuple[float, float, float, float]:
    """(x, y, z, w) quaternion for a planar yaw."""
    return 0.0, 0.0, math.sin(th / 2.0), math.cos(th / 2.0)


def limit_twist(v: float, w: float, v_max: float, w_max: float) -> Tuple[float, float]:
    """Clamp a twist to limits, scaling both to preserve curvature."""
    if not (math.isfinite(v) and math.isfinite(w)):
        return 0.0, 0.0
    s = 1.0
    if v_max > 0 and abs(v) > v_max:
        s = min(s, v_max / abs(v))
    if w_max > 0 and abs(w) > w_max:
        s = min(s, w_max / abs(w))
    return v * s, w * s
