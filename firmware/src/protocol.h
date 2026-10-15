// Petmate host <-> MCU serial protocol (docs/INTERFACES.md §2).
//
// Wire format:  COBS( [type:u8][seq:u8][payload...][crc16:u16 LE] ) 0x00
// CRC:          CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection,
//               xorout 0) computed over type|seq|payload.
// Endianness:   every multi-byte field is little-endian, float = IEEE754 binary32.
//
// This module is deliberately free of any Pico SDK dependency so that it can be
// unit-tested on the host (see firmware/test). Payloads are serialised field by
// field through ByteWriter/ByteReader; no reliance on struct layout or on the
// endianness of the machine running the code.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace proto {

// ------------------------------------------------------------ message ids --
enum class MsgType : uint8_t {
    // Host -> MCU
    CMD_VEL    = 0x01,
    FEED       = 0x02,
    LAUNCH     = 0x03,
    INTAKE     = 0x04,
    TEASE      = 0x05,
    LED        = 0x06,
    ESTOP      = 0x07,
    SET_PARAM  = 0x08,
    PING       = 0x09,
    RESET_ODOM = 0x0A,
    FEED_ABORT = 0x0B,
    TARE_SCALE = 0x0C,
    // MCU -> Host
    STATE         = 0x81,
    FEED_STATUS   = 0x82,
    LAUNCH_STATUS = 0x83,
    EVENT         = 0x84,
    PONG          = 0x85,
    LOG           = 0x86,
};

enum class ParamId : uint8_t {
    KP = 1, KI = 2, KD = 3, TICKS_PER_REV = 4, WHEEL_RADIUS_M = 5, TRACK_M = 6,
    V_MAX = 7, W_MAX = 8, A_MAX = 9, SCALE_FACTOR = 10, CLIFF_THRESHOLD_MM = 11,
};

enum class FeedState : uint8_t { IDLE = 0, DISPENSING = 1, DONE = 2, JAM = 3, HOPPER_EMPTY = 4, ABORTED = 5 };
enum class LaunchState : uint8_t { IDLE = 0, SPINNING_UP = 1, LAUNCHING = 2, DONE = 3, EMPTY = 4 };

enum class EventCode : uint8_t {
    CLIFF = 1, BUMP = 2, WHEEL_DROP = 3, MOTOR_FAULT = 4, BUTTON_SHORT = 5,
    BUTTON_LONG = 6, BUTTON_PROVISION = 7, DOCK_CONTACT = 8, UNDOCK = 9,
    BATTERY_LOW = 10, BATTERY_CRITICAL = 11, FEEDER_JAM = 12, HOPPER_LOW = 13,
    WATCHDOG = 14,
};

// STATE.flags bits
namespace flag {
constexpr uint8_t ESTOP            = 1u << 0;
constexpr uint8_t WHEEL_DROP       = 1u << 1;
constexpr uint8_t MOTOR_FAULT      = 1u << 2;
constexpr uint8_t CHARGING         = 1u << 3;
constexpr uint8_t DOCKED           = 1u << 4;
constexpr uint8_t BUTTON_PRESSED   = 1u << 5;
constexpr uint8_t WATCHDOG_TRIPPED = 1u << 6;
}  // namespace flag

// cliff_mask / bump_mask bits
namespace cliff_bit { constexpr uint8_t FL = 1u << 0, FR = 1u << 1, RL = 1u << 2, RR = 1u << 3; }
namespace bump_bit  { constexpr uint8_t FRONT = 1u << 0, REAR = 1u << 1; }

// ------------------------------------------------------------ size limits --
constexpr size_t HEADER_SIZE      = 2;   // type + seq
constexpr size_t CRC_SIZE         = 2;
constexpr size_t MAX_PAYLOAD      = 128; // LOG text <= 120 B, STATE = 52 B
constexpr size_t MAX_LOG_TEXT     = 120;
constexpr size_t MAX_RAW_FRAME    = HEADER_SIZE + MAX_PAYLOAD + CRC_SIZE;
// COBS adds at most 1 byte per 254 data bytes (+1); plus the 0x00 delimiter.
constexpr size_t cobs_max_encoded(size_t n) { return n + n / 254 + 1; }
constexpr size_t MAX_WIRE_FRAME   = cobs_max_encoded(MAX_RAW_FRAME) + 1;

// ------------------------------------------------------------------ CRC ----
uint16_t crc16_ccitt_false(const uint8_t* data, size_t len, uint16_t crc = 0xFFFF);

// ----------------------------------------------------------------- COBS ----
// Encodes `len` bytes. Output does NOT include the trailing 0x00 delimiter.
// Returns encoded length, or 0 if `out_cap` is too small.
size_t cobs_encode(const uint8_t* in, size_t len, uint8_t* out, size_t out_cap);
// Decodes one COBS block (without delimiter). Returns decoded length, or
// SIZE_MAX on malformed input / insufficient capacity.
constexpr size_t COBS_ERROR = static_cast<size_t>(-1);
size_t cobs_decode(const uint8_t* in, size_t len, uint8_t* out, size_t out_cap);

// ------------------------------------------------- little-endian helpers ---
class ByteWriter {
public:
    ByteWriter(uint8_t* buf, size_t cap) : buf_(buf), cap_(cap) {}
    void u8(uint8_t v)   { put(&v, 1); }
    void i8(int8_t v)    { u8(static_cast<uint8_t>(v)); }
    void u16(uint16_t v) { uint8_t b[2] = {uint8_t(v), uint8_t(v >> 8)}; put(b, 2); }
    void i16(int16_t v)  { u16(static_cast<uint16_t>(v)); }
    void u32(uint32_t v) { uint8_t b[4] = {uint8_t(v), uint8_t(v >> 8), uint8_t(v >> 16), uint8_t(v >> 24)}; put(b, 4); }
    void i32(int32_t v)  { u32(static_cast<uint32_t>(v)); }
    void f32(float v)    { uint32_t u; static_assert(sizeof(float) == 4, "f32"); std::memcpy(&u, &v, 4); u32(u); }
    void bytes(const uint8_t* p, size_t n) { put(p, n); }
    size_t size() const { return pos_; }
    bool ok() const { return ok_; }
private:
    void put(const uint8_t* p, size_t n) {
        if (pos_ + n > cap_) { ok_ = false; return; }
        std::memcpy(buf_ + pos_, p, n); pos_ += n;
    }
    uint8_t* buf_; size_t cap_; size_t pos_ = 0; bool ok_ = true;
};

class ByteReader {
public:
    ByteReader(const uint8_t* buf, size_t len) : buf_(buf), len_(len) {}
    uint8_t  u8()  { uint8_t b[1] = {0}; get(b, 1); return b[0]; }
    int8_t   i8()  { return static_cast<int8_t>(u8()); }
    uint16_t u16() { uint8_t b[2] = {0}; get(b, 2); return uint16_t(b[0] | (b[1] << 8)); }
    int16_t  i16() { return static_cast<int16_t>(u16()); }
    uint32_t u32() { uint8_t b[4] = {0}; get(b, 4); return uint32_t(b[0]) | (uint32_t(b[1]) << 8) | (uint32_t(b[2]) << 16) | (uint32_t(b[3]) << 24); }
    int32_t  i32() { return static_cast<int32_t>(u32()); }
    float    f32() { uint32_t u = u32(); float f; std::memcpy(&f, &u, 4); return f; }
    size_t remaining() const { return len_ - pos_; }
    bool ok() const { return ok_; }
private:
    void get(uint8_t* p, size_t n) {
        if (pos_ + n > len_) { ok_ = false; return; }
        std::memcpy(p, buf_ + pos_, n); pos_ += n;
    }
    const uint8_t* buf_; size_t len_; size_t pos_ = 0; bool ok_ = true;
};

// ------------------------------------------------------- payload structs ---
// Each struct has SIZE (exact payload length on the wire), encode() and
// decode(). decode() requires the exact length: a frame whose payload length
// does not match its type is rejected as malformed.

#define PM_PAYLOAD_COMMON(T)                                                   \
    size_t encode(uint8_t* out, size_t cap) const {                            \
        ByteWriter w(out, cap); write(w); return w.ok() ? w.size() : 0; }      \
    static bool decode(const uint8_t* in, size_t len, T& o) {                  \
        if (len != SIZE) return false;                                         \
        ByteReader r(in, len); o.read(r); return r.ok(); }

struct CmdVel {                 // 0x01
    static constexpr size_t SIZE = 4;
    int16_t v_mm_s = 0;
    int16_t w_mrad_s = 0;
    void write(ByteWriter& w) const { w.i16(v_mm_s); w.i16(w_mrad_s); }
    void read(ByteReader& r) { v_mm_s = r.i16(); w_mrad_s = r.i16(); }
    PM_PAYLOAD_COMMON(CmdVel)
};

struct Feed {                   // 0x02
    static constexpr size_t SIZE = 2;
    uint16_t grams_x10 = 0;
    void write(ByteWriter& w) const { w.u16(grams_x10); }
    void read(ByteReader& r) { grams_x10 = r.u16(); }
    PM_PAYLOAD_COMMON(Feed)
};

struct Launch {                 // 0x03
    static constexpr size_t SIZE = 2;
    uint8_t power_pct = 0;      // clamp 20..70
    uint8_t count = 0;          // 1..4
    void write(ByteWriter& w) const { w.u8(power_pct); w.u8(count); }
    void read(ByteReader& r) { power_pct = r.u8(); count = r.u8(); }
    PM_PAYLOAD_COMMON(Launch)
};

struct Intake {                 // 0x04
    static constexpr size_t SIZE = 1;
    uint8_t mode = 0;           // 0 off, 1 in, 2 out
    void write(ByteWriter& w) const { w.u8(mode); }
    void read(ByteReader& r) { mode = r.u8(); }
    PM_PAYLOAD_COMMON(Intake)
};

struct Tease {                  // 0x05
    static constexpr size_t SIZE = 3;
    uint8_t pattern = 0;        // 0 stop, 1 wiggle, 2 sweep, 3 random
    uint16_t duration_ms = 0;
    void write(ByteWriter& w) const { w.u8(pattern); w.u16(duration_ms); }
    void read(ByteReader& r) { pattern = r.u8(); duration_ms = r.u16(); }
    PM_PAYLOAD_COMMON(Tease)
};

struct Led {                    // 0x06
    static constexpr size_t SIZE = 4;
    uint8_t mode = 0, r = 0, g = 0, b = 0;
    void write(ByteWriter& w) const { w.u8(mode); w.u8(r); w.u8(g); w.u8(b); }
    void read(ByteReader& rd) { mode = rd.u8(); r = rd.u8(); g = rd.u8(); b = rd.u8(); }
    PM_PAYLOAD_COMMON(Led)
};

struct Estop {                  // 0x07
    static constexpr size_t SIZE = 1;
    uint8_t engage = 0;
    void write(ByteWriter& w) const { w.u8(engage); }
    void read(ByteReader& r) { engage = r.u8(); }
    PM_PAYLOAD_COMMON(Estop)
};

struct SetParam {               // 0x08
    static constexpr size_t SIZE = 5;
    uint8_t param_id = 0;
    float value = 0.0f;
    void write(ByteWriter& w) const { w.u8(param_id); w.f32(value); }
    void read(ByteReader& r) { param_id = r.u8(); value = r.f32(); }
    PM_PAYLOAD_COMMON(SetParam)
};

struct State {                  // 0x81
    static constexpr size_t SIZE = 52;
    uint32_t t_ms = 0;
    float x = 0, y = 0, th = 0, v = 0, w = 0;
    int32_t enc_l = 0, enc_r = 0;
    int16_t ax_mg = 0, ay_mg = 0, az_mg = 0;
    int16_t gx_cdps = 0, gy_cdps = 0, gz_cdps = 0;
    uint8_t cliff_mask = 0, bump_mask = 0, flags = 0;
    uint16_t batt_mv = 0;
    int16_t batt_ma = 0;
    uint8_t soc_pct = 0;
    void write(ByteWriter& o) const {
        o.u32(t_ms); o.f32(x); o.f32(y); o.f32(th); o.f32(v); o.f32(w);
        o.i32(enc_l); o.i32(enc_r);
        o.i16(ax_mg); o.i16(ay_mg); o.i16(az_mg);
        o.i16(gx_cdps); o.i16(gy_cdps); o.i16(gz_cdps);
        o.u8(cliff_mask); o.u8(bump_mask); o.u8(flags);
        o.u16(batt_mv); o.i16(batt_ma); o.u8(soc_pct);
    }
    void read(ByteReader& r) {
        t_ms = r.u32(); x = r.f32(); y = r.f32(); th = r.f32(); v = r.f32(); w = r.f32();
        enc_l = r.i32(); enc_r = r.i32();
        ax_mg = r.i16(); ay_mg = r.i16(); az_mg = r.i16();
        gx_cdps = r.i16(); gy_cdps = r.i16(); gz_cdps = r.i16();
        cliff_mask = r.u8(); bump_mask = r.u8(); flags = r.u8();
        batt_mv = r.u16(); batt_ma = r.i16(); soc_pct = r.u8();
    }
    PM_PAYLOAD_COMMON(State)
};

struct FeedStatus {             // 0x82
    static constexpr size_t SIZE = 9;
    uint8_t state = 0;
    float dispensed_g = 0, hopper_g = 0;
    void write(ByteWriter& w) const { w.u8(state); w.f32(dispensed_g); w.f32(hopper_g); }
    void read(ByteReader& r) { state = r.u8(); dispensed_g = r.f32(); hopper_g = r.f32(); }
    PM_PAYLOAD_COMMON(FeedStatus)
};

struct LaunchStatus {           // 0x83
    static constexpr size_t SIZE = 2;
    uint8_t state = 0, balls = 0;
    void write(ByteWriter& w) const { w.u8(state); w.u8(balls); }
    void read(ByteReader& r) { state = r.u8(); balls = r.u8(); }
    PM_PAYLOAD_COMMON(LaunchStatus)
};

struct Event {                  // 0x84
    static constexpr size_t SIZE = 3;
    uint8_t code = 0;
    uint16_t data = 0;
    void write(ByteWriter& w) const { w.u8(code); w.u16(data); }
    void read(ByteReader& r) { code = r.u8(); data = r.u16(); }
    PM_PAYLOAD_COMMON(Event)
};

struct Pong {                   // 0x85
    static constexpr size_t SIZE = 7;
    uint8_t fw_major = 0, fw_minor = 0, fw_patch = 0;
    uint32_t uptime_ms = 0;
    void write(ByteWriter& w) const { w.u8(fw_major); w.u8(fw_minor); w.u8(fw_patch); w.u32(uptime_ms); }
    void read(ByteReader& r) { fw_major = r.u8(); fw_minor = r.u8(); fw_patch = r.u8(); uptime_ms = r.u32(); }
    PM_PAYLOAD_COMMON(Pong)
};

#undef PM_PAYLOAD_COMMON

// ---------------------------------------------------------- frame build ----
// Builds a complete wire frame (COBS-encoded, terminated by 0x00) into `out`.
// Returns total bytes written including the delimiter, or 0 on error.
size_t build_frame(uint8_t type, uint8_t seq, const uint8_t* payload, size_t payload_len,
                   uint8_t* out, size_t out_cap);

template <typename T>
size_t build_msg(MsgType type, uint8_t seq, const T& msg, uint8_t* out, size_t out_cap) {
    uint8_t pl[T::SIZE];
    if (msg.encode(pl, sizeof pl) != T::SIZE) return 0;
    return build_frame(static_cast<uint8_t>(type), seq, pl, T::SIZE, out, out_cap);
}

// ------------------------------------------------------- frame decoding ----
struct Frame {
    uint8_t type = 0;
    uint8_t seq = 0;
    uint8_t payload[MAX_PAYLOAD] = {};
    size_t  payload_len = 0;
};

// Incremental stream decoder: feed received bytes one at a time.
class FrameDecoder {
public:
    enum class Result { NONE, FRAME, ERROR };
    // Returns FRAME when `out` has been filled with a valid frame, ERROR when a
    // delimiter closed a malformed frame (COBS, length or CRC), NONE otherwise.
    Result push(uint8_t byte, Frame& out);
    void reset() { len_ = 0; overflow_ = false; }
    uint32_t crc_errors() const { return crc_errors_; }
    uint32_t framing_errors() const { return framing_errors_; }
private:
    uint8_t buf_[cobs_max_encoded(MAX_RAW_FRAME)] = {};
    size_t len_ = 0;
    bool overflow_ = false;
    uint32_t crc_errors_ = 0;
    uint32_t framing_errors_ = 0;
};

}  // namespace proto
