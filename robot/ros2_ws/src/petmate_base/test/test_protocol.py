import math
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from petmate_base import protocol as P  # noqa: E402
from petmate_base.odometry import DiffDriveOdometry  # noqa: E402


def test_crc16_ccitt_false_check_value():
    # Standard check value for CRC-16/CCITT-FALSE
    assert P.crc16_ccitt_false(b'123456789') == 0x29B1
    assert P.crc16_ccitt_false(b'') == 0xFFFF


@pytest.mark.parametrize('data,enc', [
    (b'', b'\x01'),
    (b'\x00', b'\x01\x01'),
    (b'\x00\x00', b'\x01\x01\x01'),
    (b'\x11\x22\x00\x33', b'\x03\x11\x22\x02\x33'),
    (b'\x11\x22\x33\x44', b'\x05\x11\x22\x33\x44'),
    (b'\x11\x00\x00\x00', b'\x02\x11\x01\x01\x01'),
])
def test_cobs_vectors(data, enc):
    assert P.cobs_encode(data) == enc
    assert P.cobs_decode(enc) == data


@pytest.mark.parametrize('n', [253, 254, 255, 256, 509, 510, 600])
def test_cobs_long_roundtrip(n):
    data = bytes((i % 255) + 1 for i in range(n))
    enc = P.cobs_encode(data)
    assert 0 not in enc
    assert P.cobs_decode(enc) == data
    data2 = bytes(i % 256 for i in range(n))
    assert P.cobs_decode(P.cobs_encode(data2)) == data2


def test_cobs_254_nonzero_block():
    data = bytes([1] * 254)
    assert P.cobs_encode(data) == b'\xff' + data + b'\x01'


def test_cobs_decode_errors():
    with pytest.raises(ValueError):
        P.cobs_decode(b'\x05\x11\x22')
    with pytest.raises(ValueError):
        P.cobs_decode(b'\x03\x00\x01')


def test_frame_layout_cmd_vel():
    frame = P.build_frame(P.CMD_VEL, 7, P.enc_cmd_vel(0.25, -1.5))
    assert frame.endswith(b'\x00') and 0 not in frame[:-1]
    raw = P.cobs_decode(frame[:-1])
    assert raw[0] == 0x01 and raw[1] == 7
    v, w = struct.unpack('<hh', raw[2:6])
    assert (v, w) == (250, -1500)
    (crc,) = struct.unpack('<H', raw[6:8])
    assert crc == P.crc16_ccitt_false(raw[:6])
    assert P.parse_frame(frame[:-1]) == (0x01, 7, raw[2:6])


def test_ping_has_empty_payload():
    raw = P.cobs_decode(P.build_frame(P.PING, 255)[:-1])
    assert len(raw) == 4 and raw[0] == 0x09 and raw[1] == 255


def test_payload_encoders():
    assert P.enc_feed(25.0) == struct.pack('<H', 250)
    assert P.enc_launch(10, 9) == bytes([20, 4])
    assert P.enc_launch(90, 0) == bytes([70, 1])
    assert P.enc_intake(2) == b'\x02'
    assert P.enc_tease(1, 20000) == struct.pack('<BH', 1, 20000)
    assert P.enc_tease(3, 999999) == struct.pack('<BH', 3, 65535)
    assert P.enc_led(4, 0xF0, 0xA8, 0x30) == bytes([4, 0xF0, 0xA8, 0x30])
    assert P.enc_estop(True) == b'\x01'
    assert P.enc_set_param(4, 1320.0) == struct.pack('<Bf', 4, 1320.0)
    assert len(P.enc_set_param(4, 1.0)) == 5
    assert P.enc_cmd_vel(100.0, float('nan')) == struct.pack('<hh', 0, 0)
    assert P.enc_cmd_vel(100.0, 0) == struct.pack('<hh', 32767, 0)
    with pytest.raises(ValueError):
        P.enc_intake(3)


def test_hex_color():
    assert P.parse_hex_color('#F0A830') == (0xF0, 0xA8, 0x30)
    assert P.parse_hex_color('fff') == (255, 255, 255)
    with pytest.raises(ValueError):
        P.parse_hex_color('#12')


def _sample_state(**kw):
    d = dict(t_ms=123456, x=1.5, y=-0.25, th=0.5, v=0.2, w=-0.1, enc_l=-5,
             enc_r=100000, ax_mg=10, ay_mg=-20, az_mg=1000, gx_cdps=0,
             gy_cdps=-100, gz_cdps=9000, cliff_mask=0b0101, bump_mask=0b10,
             flags=P.FLAG_DOCKED | P.FLAG_CHARGING, batt_mv=15600,
             batt_ma=-1200, soc_pct=82)
    d.update(kw)
    return P.State(**d)


def test_state_size_and_roundtrip():
    assert P.STATE_SIZE == 52
    s = _sample_state()
    payload = P.enc_state(s)
    assert len(payload) == 52
    d = P.dec_state(payload)
    assert d.t_ms == 123456 and d.enc_r == 100000 and d.batt_ma == -1200
    assert math.isclose(d.x, 1.5) and math.isclose(d.th, 0.5, rel_tol=1e-6)
    assert d.docked and d.charging and not d.estop
    gx, gy, gz = d.gyro_rad_s()
    assert math.isclose(gz, math.radians(90.0), rel_tol=1e-9)
    assert math.isclose(d.accel_m_s2()[2], 9.80665)
    with pytest.raises(ValueError):
        P.dec_state(payload[:-1])


def test_state_field_offsets():
    # Explicit offsets per INTERFACES.md §2 (little-endian, packed)
    p = P.enc_state(_sample_state())
    assert struct.unpack_from('<I', p, 0)[0] == 123456
    assert struct.unpack_from('<i', p, 24)[0] == -5          # enc_l
    assert struct.unpack_from('<h', p, 42)[0] == 9000        # gz_cdps
    assert p[44] == 0b0101 and p[45] == 0b10                 # cliff, bump
    assert p[46] == (P.FLAG_DOCKED | P.FLAG_CHARGING)        # flags
    assert struct.unpack_from('<H', p, 47)[0] == 15600       # batt_mv
    assert struct.unpack_from('<h', p, 49)[0] == -1200       # batt_ma
    assert p[51] == 82                                       # soc


def test_other_mcu_payloads():
    fs = P.dec_feed_status(struct.pack('<Bff', 2, 24.5, 180.0))
    assert fs.state_name == 'done' and math.isclose(fs.dispensed_g, 24.5)
    ls = P.dec_launch_status(bytes([4, 0]))
    assert ls.state_name == 'empty' and ls.balls == 0
    ev = P.dec_event(struct.pack('<BH', 13, 40))
    assert ev.name == 'hopper_low' and ev.data == 40
    po = P.dec_pong(struct.pack('<BBBI', 0, 1, 0, 5000))
    assert po.fw == '0.1.0' and po.uptime_ms == 5000
    assert P.decode_payload(P.LOG, b'hello') == 'hello'
    with pytest.raises(ValueError):
        P.decode_payload(0x99, b'')


def test_frame_reader_stream_with_noise_and_bad_crc():
    good1 = P.build_frame(P.STATE, 1, P.enc_state(_sample_state()))
    good2 = P.build_frame(P.EVENT, 2, struct.pack('<BH', 8, 0))
    bad = bytearray(P.build_frame(P.PONG, 3, struct.pack('<BBBI', 0, 1, 0, 1)))
    bad[3] ^= 0x55  # corrupt payload byte (still nonzero after xor? ensure)
    if bad[3] == 0:
        bad[3] = 1
    stream = b'\x00\x00' + good1 + bytes(bad) + good2
    r = P.FrameReader()
    out = []
    # feed in awkward chunk sizes
    for i in range(0, len(stream), 7):
        out.extend(r.feed(stream[i:i + 7]))
    assert [t for t, _, _ in out] == [P.STATE, P.EVENT]
    assert r.errors == 1 and r.frames == 2


def test_frame_reader_overflow_resync():
    r = P.FrameReader(max_frame=16)
    junk = bytes([1] * 40)
    out = list(r.feed(junk + b'\x00' + P.build_frame(P.PING, 0)))
    assert out == [(P.PING, 0, b'')]
    assert r.errors >= 1


def test_seq_wraps():
    s = P.SeqCounter()
    vals = [s.next() for _ in range(257)]
    assert vals[0] == 0 and vals[255] == 255 and vals[256] == 0


def test_limit_twist_preserves_curvature():
    v, w = P.limit_twist(1.0, 1.0, 0.5, 2.0)
    assert math.isclose(v, 0.5) and math.isclose(w, 0.5)
    v, w = P.limit_twist(0.1, 4.0, 0.5, 2.0)
    assert math.isclose(w, 2.0) and math.isclose(v, 0.05)
    assert P.limit_twist(float('inf'), 0, 1, 1) == (0.0, 0.0)


def test_diff_drive_odometry_straight_and_turn():
    o = DiffDriveOdometry(1320, 0.036, 0.26)
    o.update(0, 0, 0.0)
    # one wheel revolution both sides -> 2*pi*r forward
    x, y, th, v, w = o.update(1320, 1320, 1.0)
    assert math.isclose(x, 2 * math.pi * 0.036, rel_tol=1e-9)
    assert abs(y) < 1e-12 and abs(th) < 1e-12
    assert math.isclose(v, 2 * math.pi * 0.036)
    # spin in place by pi/2: arc per wheel = track/2 * pi/2
    o.reset()
    o.update(0, 0, 0.0)
    ticks = (0.13 * math.pi / 2) / o.m_per_tick
    o.update(-int(round(ticks)), int(round(ticks)), 1.0)
    assert math.isclose(o.th, math.pi / 2, abs_tol=2e-3)
    assert abs(o.x) < 1e-9


def test_odometry_encoder_wrap():
    o = DiffDriveOdometry(1320, 0.036, 0.26)
    o.update(2**31 - 10, 2**31 - 10, 0.0)
    o.update(-(2**31) + 10, -(2**31) + 10, 0.1)  # +20 ticks across wrap
    assert math.isclose(o.x, 20 * o.m_per_tick, rel_tol=1e-9)
