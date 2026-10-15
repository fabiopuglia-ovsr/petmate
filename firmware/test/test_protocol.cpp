// Host-side unit tests for the Petmate protocol and control math.
// Builds with plain gcc/CMake, no Pico SDK. Run: ctest or ./petmate_tests
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <vector>

#include "control.h"
#include "protocol.h"

using namespace proto;

static int g_fail = 0, g_pass = 0;
#define CHECK(cond)                                                                   \
    do {                                                                              \
        if (cond) ++g_pass;                                                           \
        else { ++g_fail; std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
    } while (0)

static std::vector<uint8_t> cobs(const std::vector<uint8_t>& in) {
    std::vector<uint8_t> out(cobs_max_encoded(in.size()));
    size_t n = cobs_encode(in.data(), in.size(), out.data(), out.size());
    out.resize(n);
    return out;
}

static void test_crc() {
    // Standard check value for CRC-16/CCITT-FALSE.
    const char* s = "123456789";
    CHECK(crc16_ccitt_false(reinterpret_cast<const uint8_t*>(s), 9) == 0x29B1);
    CHECK(crc16_ccitt_false(nullptr, 0) == 0xFFFF);
    // Incremental computation equals one-shot.
    uint16_t c = crc16_ccitt_false(reinterpret_cast<const uint8_t*>(s), 4);
    c = crc16_ccitt_false(reinterpret_cast<const uint8_t*>(s) + 4, 5, c);
    CHECK(c == 0x29B1);
}

static void test_cobs_vectors() {
    // Vectors from the COBS paper / Wikipedia.
    struct V { std::vector<uint8_t> in, out; };
    std::vector<V> vs = {
        {{0x00}, {0x01, 0x01}},
        {{0x00, 0x00}, {0x01, 0x01, 0x01}},
        {{0x00, 0x11, 0x00}, {0x01, 0x02, 0x11, 0x01}},
        {{0x11, 0x22, 0x00, 0x33}, {0x03, 0x11, 0x22, 0x02, 0x33}},
        {{0x11, 0x22, 0x33, 0x44}, {0x05, 0x11, 0x22, 0x33, 0x44}},
        {{0x11, 0x00, 0x00, 0x00}, {0x02, 0x11, 0x01, 0x01, 0x01}},
        {{}, {0x01}},
    };
    for (auto& v : vs) {
        auto enc = cobs(v.in);
        CHECK(enc == v.out);
        std::vector<uint8_t> dec(v.in.size() + 4);
        size_t n = cobs_decode(enc.data(), enc.size(), dec.data(), dec.size());
        CHECK(n == v.in.size());
        dec.resize(n == COBS_ERROR ? 0 : n);
        CHECK(dec == v.in);
    }
    // 254 non-zero bytes -> single max block; 255 -> two blocks.
    std::vector<uint8_t> a(254);
    for (size_t i = 0; i < a.size(); ++i) a[i] = uint8_t(i + 1);
    auto ea = cobs(a);
    CHECK(ea.size() == 255 && ea[0] == 0xFF);
    std::vector<uint8_t> b(255);
    for (size_t i = 0; i < b.size(); ++i) b[i] = uint8_t((i % 255) + 1);
    auto eb = cobs(b);
    CHECK(eb.size() == 257 && eb[0] == 0xFF && eb[255] == 0x02);
    // Malformed: embedded zero, truncated block.
    uint8_t bad1[] = {0x03, 0x11, 0x00};
    uint8_t out[16];
    CHECK(cobs_decode(bad1, sizeof bad1, out, sizeof out) == COBS_ERROR);
    uint8_t bad2[] = {0x05, 0x11, 0x22};
    CHECK(cobs_decode(bad2, sizeof bad2, out, sizeof out) == COBS_ERROR);
}

static void test_cobs_fuzz() {
    std::mt19937 rng(1234);
    for (int it = 0; it < 5000; ++it) {
        size_t len = rng() % 600;
        std::vector<uint8_t> in(len);
        for (auto& x : in) x = (rng() % 4 == 0) ? 0 : uint8_t(rng());
        auto enc = cobs(in);
        bool nozero = true;
        for (auto x : enc) nozero &= (x != 0);
        CHECK(nozero);
        CHECK(enc.size() <= cobs_max_encoded(len));
        std::vector<uint8_t> dec(len + 1);
        size_t n = cobs_decode(enc.data(), enc.size(), dec.data(), dec.size());
        CHECK(n == len);
        if (n == len && len > 0) CHECK(std::memcmp(dec.data(), in.data(), len) == 0);
    }
}

static void test_payload_sizes() {
    CHECK(CmdVel::SIZE == 4);
    CHECK(Feed::SIZE == 2);
    CHECK(Launch::SIZE == 2);
    CHECK(Intake::SIZE == 1);
    CHECK(Tease::SIZE == 3);
    CHECK(Led::SIZE == 4);
    CHECK(Estop::SIZE == 1);
    CHECK(SetParam::SIZE == 5);
    // STATE: u32 + 5*f32 + 2*i32 + 6*i16 + 3*u8 + u16 + i16 + u8 = 4+20+8+12+3+2+2+1
    CHECK(State::SIZE == 52);
    CHECK(FeedStatus::SIZE == 9);
    CHECK(LaunchStatus::SIZE == 2);
    CHECK(Event::SIZE == 3);
    CHECK(Pong::SIZE == 7);
    // encode() must produce exactly SIZE bytes for each type.
    uint8_t buf[64];
    CHECK(State{}.encode(buf, sizeof buf) == State::SIZE);
    CHECK(CmdVel{}.encode(buf, sizeof buf) == CmdVel::SIZE);
    CHECK(SetParam{}.encode(buf, sizeof buf) == SetParam::SIZE);
    CHECK(FeedStatus{}.encode(buf, sizeof buf) == FeedStatus::SIZE);
    CHECK(Pong{}.encode(buf, sizeof buf) == Pong::SIZE);
    // Too-small buffer fails cleanly.
    CHECK(State{}.encode(buf, 10) == 0);
}

static void test_little_endian_layout() {
    CmdVel c; c.v_mm_s = 300; c.w_mrad_s = -2;
    uint8_t b[4];
    c.encode(b, 4);
    CHECK(b[0] == 0x2C && b[1] == 0x01 && b[2] == 0xFE && b[3] == 0xFF);

    SetParam p; p.param_id = 1; p.value = 1.0f;  // 0x3F800000
    uint8_t q[5];
    p.encode(q, 5);
    CHECK(q[0] == 1 && q[1] == 0x00 && q[2] == 0x00 && q[3] == 0x80 && q[4] == 0x3F);

    State s; s.t_ms = 0x04030201; s.batt_mv = 15600; s.soc_pct = 82; s.flags = flag::DOCKED;
    uint8_t sb[State::SIZE];
    s.encode(sb, sizeof sb);
    CHECK(sb[0] == 1 && sb[1] == 2 && sb[2] == 3 && sb[3] == 4);
    // Offsets: flags@46, batt_mv@47..48, batt_ma@49..50, soc@51
    CHECK(sb[46] == flag::DOCKED);
    CHECK(sb[47] == (15600 & 0xFF) && sb[48] == (15600 >> 8));
    CHECK(sb[51] == 82);
}

static void test_frame_roundtrip() {
    // Golden vector shared with tools/mcu_console.py --selftest:
    // PING seq=7 -> raw [09 07] crc -> COBS -> 00
    uint8_t wire[MAX_WIRE_FRAME];
    size_t n = build_frame(uint8_t(MsgType::PING), 7, nullptr, 0, wire, sizeof wire);
    uint8_t raw_ping[2] = {0x09, 0x07};
    uint16_t crc = crc16_ccitt_false(raw_ping, 2);
    CHECK(n == 6);
    CHECK(wire[0] == 0x05 && wire[1] == 0x09 && wire[2] == 0x07);
    CHECK(wire[3] == uint8_t(crc) && wire[4] == uint8_t(crc >> 8));
    CHECK(wire[5] == 0x00);
    std::printf("golden PING seq=7: ");
    for (size_t i = 0; i < n; ++i) std::printf("%02x", wire[i]);
    std::printf("\n");

    // CMD_VEL golden vector (contains zero bytes in payload).
    CmdVel cv; cv.v_mm_s = 200; cv.w_mrad_s = 0;
    n = build_msg(MsgType::CMD_VEL, 1, cv, wire, sizeof wire);
    std::printf("golden CMD_VEL v=200 w=0 seq=1: ");
    for (size_t i = 0; i < n; ++i) std::printf("%02x", wire[i]);
    std::printf("\n");

    FrameDecoder dec;
    Frame f;
    int frames = 0;
    for (size_t i = 0; i < n; ++i) {
        auto r = dec.push(wire[i], f);
        if (r == FrameDecoder::Result::FRAME) ++frames;
    }
    CHECK(frames == 1);
    CHECK(f.type == 0x01 && f.seq == 1 && f.payload_len == 4);
    CmdVel back;
    CHECK(CmdVel::decode(f.payload, f.payload_len, back));
    CHECK(back.v_mm_s == 200 && back.w_mrad_s == 0);
    // Wrong length is rejected.
    CHECK(!CmdVel::decode(f.payload, 3, back));

    // Full STATE roundtrip through the stream decoder.
    State s;
    s.t_ms = 123456; s.x = 1.25f; s.y = -0.5f; s.th = 3.0f; s.v = 0.2f; s.w = -1.0f;
    s.enc_l = -100000; s.enc_r = 2000000; s.ax_mg = -1000; s.az_mg = 981; s.gz_cdps = -4500;
    s.cliff_mask = 0x0F; s.bump_mask = 3; s.flags = 0x7F; s.batt_mv = 16800; s.batt_ma = -1200; s.soc_pct = 99;
    n = build_msg(MsgType::STATE, 250, s, wire, sizeof wire);
    CHECK(n > 0 && n <= MAX_WIRE_FRAME);
    // Golden STATE frame, byte-identical to tools/mcu_console.py output for
    // the same field values (cross-implementation check).
    static const char* golden_state =
        "0681fa40e201010103a03f010102bf01074040cdcc4c3e010a80bf6079feff80841e0318fc0103d5030101010d6cee0f037fa04150fb63f06f00";
    char hex[2 * MAX_WIRE_FRAME + 1] = {0};
    for (size_t i = 0; i < n; ++i) std::snprintf(hex + 2 * i, 3, "%02x", wire[i]);
    CHECK(std::strcmp(hex, golden_state) == 0);
    frames = 0;
    for (size_t i = 0; i < n; ++i)
        if (dec.push(wire[i], f) == FrameDecoder::Result::FRAME) ++frames;
    CHECK(frames == 1 && f.type == 0x81 && f.seq == 250);
    State s2;
    CHECK(State::decode(f.payload, f.payload_len, s2));
    CHECK(s2.t_ms == s.t_ms && s2.x == s.x && s2.y == s.y && s2.th == s.th && s2.v == s.v && s2.w == s.w);
    CHECK(s2.enc_l == s.enc_l && s2.enc_r == s.enc_r && s2.ax_mg == s.ax_mg && s2.az_mg == s.az_mg);
    CHECK(s2.gz_cdps == s.gz_cdps && s2.cliff_mask == s.cliff_mask && s2.bump_mask == s.bump_mask);
    CHECK(s2.flags == s.flags && s2.batt_mv == s.batt_mv && s2.batt_ma == s.batt_ma && s2.soc_pct == s.soc_pct);
}

static void test_decoder_errors_and_resync() {
    uint8_t wire[MAX_WIRE_FRAME];
    FrameDecoder dec;
    Frame f;
    Feed fd; fd.grams_x10 = 250;
    size_t n = build_msg(MsgType::FEED, 3, fd, wire, sizeof wire);
    // Corrupt a payload byte -> CRC error.
    std::vector<uint8_t> bad(wire, wire + n);
    bad[3] ^= 0x40;
    if (bad[3] == 0) bad[3] = 0x01;
    FrameDecoder::Result last = FrameDecoder::Result::NONE;
    for (auto b : bad) { auto r = dec.push(b, f); if (r != FrameDecoder::Result::NONE) last = r; }
    CHECK(last == FrameDecoder::Result::ERROR);
    CHECK(dec.crc_errors() + dec.framing_errors() == 1);
    // Garbage then a good frame -> decoder resynchronises at the delimiter.
    const uint8_t junk[] = {0x13, 0x37, 0xAA, 0x00};
    for (auto b : junk) dec.push(b, f);
    int frames = 0;
    for (size_t i = 0; i < n; ++i)
        if (dec.push(wire[i], f) == FrameDecoder::Result::FRAME) ++frames;
    CHECK(frames == 1 && f.type == 0x02);
    Feed fd2;
    CHECK(Feed::decode(f.payload, f.payload_len, fd2) && fd2.grams_x10 == 250);
    // Over-long garbage without delimiter is bounded and reported.
    for (int i = 0; i < 1000; ++i) dec.push(0x55, f);
    CHECK(dec.push(0x00, f) == FrameDecoder::Result::ERROR);
    // Too-short frame (only 2 bytes decoded) is rejected.
    const uint8_t shortf[] = {0x03, 0x09, 0x07, 0x00};
    CHECK(dec.push(shortf[0], f) == FrameDecoder::Result::NONE);
    dec.push(shortf[1], f); dec.push(shortf[2], f);
    CHECK(dec.push(shortf[3], f) == FrameDecoder::Result::ERROR);
    // Random fuzz across all message types.
    std::mt19937 rng(42);
    int ok = 0;
    for (int it = 0; it < 2000; ++it) {
        uint8_t pl[MAX_PAYLOAD];
        size_t len = rng() % (MAX_PAYLOAD + 1);
        for (size_t i = 0; i < len; ++i) pl[i] = uint8_t(rng());
        uint8_t type = uint8_t(rng()), seq = uint8_t(rng());
        size_t m = build_frame(type, seq, pl, len, wire, sizeof wire);
        if (m == 0) continue;
        for (size_t i = 0; i < m; ++i)
            if (dec.push(wire[i], f) == FrameDecoder::Result::FRAME &&
                f.type == type && f.seq == seq && f.payload_len == len &&
                (len == 0 || std::memcmp(f.payload, pl, len) == 0)) ++ok;
    }
    CHECK(ok == 2000);
}

static void test_control_math() {
    using namespace ctl;
    CHECK(std::fabs(wrap_angle(3.5f) - (3.5f - 2 * PI_F)) < 1e-5f);
    CHECK(std::fabs(wrap_angle(-3.5f) - (-3.5f + 2 * PI_F)) < 1e-5f);
    CHECK(slew(0.f, 1.f, 0.6f, 0.005f) == 0.6f * 0.005f);
    CHECK(slew(0.5f, 0.5f, 0.6f, 0.005f) == 0.5f);
    // Odometry: drive straight 1 m, then rotate in place by pi/2.
    Odometry o;
    for (int i = 0; i < 1000; ++i) o.update(0.001f, 0.001f, 0.26f, 0.005f);
    CHECK(std::fabs(o.x - 1.0f) < 1e-3f && std::fabs(o.y) < 1e-5f);
    CHECK(std::fabs(o.v - 0.2f) < 1e-3f);
    const float arc = (PI_F / 2) * 0.13f;  // wheel travel for 90 deg
    for (int i = 0; i < 100; ++i) o.update(-arc / 100, arc / 100, 0.26f, 0.005f);
    CHECK(std::fabs(o.th - PI_F / 2) < 1e-4f && std::fabs(o.x - 1.0f) < 1e-3f);
    // Wheel kinematics.
    float vl, vr;
    twist_to_wheels(0.2f, 1.0f, 0.26f, vl, vr);
    CHECK(std::fabs(vl - 0.07f) < 1e-6f && std::fabs(vr - 0.33f) < 1e-6f);
    // PID converges a first-order plant and respects limits.
    Pid p; p.kp = 0.8f; p.ki = 6.f; p.out_min = -1; p.out_max = 1;
    float y = 0.f;
    for (int i = 0; i < 2000; ++i) { float u = p.update(0.3f, y, 0.f, 0.005f); y += (u * 0.8f - y) * 0.005f / 0.05f; }
    CHECK(std::fabs(y - 0.3f) < 1e-3f);
    // SoC: 4S full / empty / IR compensation raises SoC under load.
    CHECK(soc_from_voltage(16.8f, 0.f, 0.12f) == 100);
    CHECK(soc_from_voltage(12.0f, 0.f, 0.12f) == 0);
    uint8_t s_rest = soc_from_voltage(15.16f, 0.f, 0.12f);   // 3.79 V/cell = 50 %
    uint8_t s_load = soc_from_voltage(14.92f, -2.f, 0.12f);  // same OCV under 2 A load
    CHECK(s_rest == 50 && s_load == 50);
    // Sharp: monotonic, saturations.
    CHECK(sharp_gp2y0a51_mm(0.87f) == 60.f);
    CHECK(sharp_gp2y0a51_mm(0.2f) >= 150.f);
    CHECK(sharp_gp2y0a51_mm(3.0f) == 15.f);
    CHECK(sharp_gp2y0a51_mm(1.0f) > sharp_gp2y0a51_mm(1.1f));
}

int main() {
    test_crc();
    test_cobs_vectors();
    test_cobs_fuzz();
    test_payload_sizes();
    test_little_endian_layout();
    test_frame_roundtrip();
    test_decoder_errors_and_resync();
    test_control_math();
    std::printf("%d passed, %d failed\n", g_pass, g_fail);
    return g_fail ? EXIT_FAILURE : EXIT_SUCCESS;
}
