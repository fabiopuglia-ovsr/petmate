#include "params.h"

#include <cmath>
#include <cstring>

#include "config.h"
#include "hardware/flash.h"
#include "pico/flash.h"
#include "protocol.h"

namespace params {
namespace {

constexpr uint32_t MAGIC = 0x504D5031;  // "PMP1"
constexpr uint32_t FLASH_OFFSET = PICO_FLASH_SIZE_BYTES - FLASH_SECTOR_SIZE;
constexpr uint32_t SAVE_DELAY_MS = 2000;  // coalesce bursts of SET_PARAM

struct Record {
    uint32_t magic;
    uint32_t size;
    Params p;
    uint16_t crc;
};

Params g_p;
bool g_dirty = false;
bool g_loaded = false;
uint32_t g_dirty_since_ms = 0;

Params defaults() {
    Params p{};
    // TODO(calibration): tune PID on the real drivetrain (start from FF only).
    p.kp = 1.0f;
    p.ki = 8.0f;
    p.kd = 0.0f;
    p.ticks_per_rev = cfg::TICKS_PER_REV;
    p.wheel_radius_m = cfg::WHEEL_DIAMETER_M * 0.5f;
    p.track_m = cfg::TRACK_M;
    p.v_max = cfg::V_MAX_DEFAULT;
    p.w_max = cfg::W_MAX_DEFAULT;
    p.a_max = cfg::A_MAX_DEFAULT;
    // TODO(calibration): HX711 counts per gram for the installed load cell
    // (typ. ~420 for a 5 kg / 1 mV/V cell at gain 128). Use SET_PARAM 10.
    p.scale_factor = 420.0f;
    p.cliff_threshold_mm = 60.0f;
    p.scale_tare_offset = 0;
    return p;
}

uint16_t record_crc(const Record& r) {
    return proto::crc16_ccitt_false(reinterpret_cast<const uint8_t*>(&r), offsetof(Record, crc));
}

bool valid(const Params& p) {
    const float* f = &p.kp;
    for (int i = 0; i < 11; ++i) if (!std::isfinite(f[i])) return false;
    return true;
}

void do_flash_write(void* arg) {
    const uint8_t* page_buf = static_cast<const uint8_t*>(arg);
    flash_range_erase(FLASH_OFFSET, FLASH_SECTOR_SIZE);
    flash_range_program(FLASH_OFFSET, page_buf, FLASH_PAGE_SIZE);
}

}  // namespace

void init() {
    g_p = defaults();
    const auto* stored = reinterpret_cast<const Record*>(XIP_BASE + FLASH_OFFSET);
    Record r;
    std::memcpy(&r, stored, sizeof r);
    if (r.magic == MAGIC && r.size == sizeof(Params) && r.crc == record_crc(r) && valid(r.p)) {
        // Re-validate every field through set() so that tightened limits in a
        // newer firmware are enforced on old records.
        const float vals[11] = {r.p.kp, r.p.ki, r.p.kd, r.p.ticks_per_rev, r.p.wheel_radius_m, r.p.track_m,
                                r.p.v_max, r.p.w_max, r.p.a_max, r.p.scale_factor, r.p.cliff_threshold_mm};
        for (uint8_t id = 1; id <= 11; ++id) set(id, vals[id - 1], nullptr);
        g_p.scale_tare_offset = r.p.scale_tare_offset;
        g_loaded = true;
    }
    g_dirty = false;
}

const Params& get() { return g_p; }

bool set(uint8_t id, float v, const char** reason) {
    const char* why = nullptr;
    auto in = [&](float lo, float hi) {
        if (!std::isfinite(v) || v < lo || v > hi) { why = "out of range"; return false; }
        return true;
    };
    using proto::ParamId;
    switch (static_cast<ParamId>(id)) {
        case ParamId::KP:             if (in(0.f, 20.f)) g_p.kp = v; break;
        case ParamId::KI:             if (in(0.f, 200.f)) g_p.ki = v; break;
        case ParamId::KD:             if (in(0.f, 2.f)) g_p.kd = v; break;
        case ParamId::TICKS_PER_REV:  if (in(16.f, 1e6f)) g_p.ticks_per_rev = v; break;
        case ParamId::WHEEL_RADIUS_M: if (in(0.01f, 0.2f)) g_p.wheel_radius_m = v; break;
        case ParamId::TRACK_M:        if (in(0.1f, 0.6f)) g_p.track_m = v; break;
        // Motion limits cannot exceed the spec hard limits (§1).
        case ParamId::V_MAX:          if (in(0.f, cfg::V_MAX_DEFAULT)) g_p.v_max = v; break;
        case ParamId::W_MAX:          if (in(0.f, cfg::W_MAX_DEFAULT)) g_p.w_max = v; break;
        case ParamId::A_MAX:          if (in(0.05f, cfg::A_MAX_DEFAULT)) g_p.a_max = v; break;
        case ParamId::SCALE_FACTOR:
            if (!std::isfinite(v) || std::fabs(v) < 1.f || std::fabs(v) > 1e6f) why = "out of range";
            else g_p.scale_factor = v;
            break;
        case ParamId::CLIFF_THRESHOLD_MM: if (in(20.f, 150.f)) g_p.cliff_threshold_mm = v; break;
        default: why = "unknown id"; break;
    }
    if (reason) *reason = why;
    if (why) return false;
    if (!g_dirty) g_dirty_since_ms = 0;
    g_dirty = true;
    return true;
}

void set_tare_offset(int32_t raw) {
    g_p.scale_tare_offset = raw;
    g_dirty = true;
    g_dirty_since_ms = 0;
}

void save_if_dirty(bool safe_now, uint32_t now_ms) {
    if (!g_dirty) return;
    if (g_dirty_since_ms == 0) { g_dirty_since_ms = now_ms ? now_ms : 1; return; }
    if (now_ms - g_dirty_since_ms < SAVE_DELAY_MS || !safe_now) return;
    static_assert(sizeof(Record) <= FLASH_PAGE_SIZE, "record must fit one page");
    alignas(4) static uint8_t page[FLASH_PAGE_SIZE];
    std::memset(page, 0xFF, sizeof page);
    Record r{};
    r.magic = MAGIC;
    r.size = sizeof(Params);
    r.p = g_p;
    r.crc = record_crc(r);
    std::memcpy(page, &r, sizeof r);
    // flash_safe_execute disables interrupts (and parks core 1 if used).
    if (flash_safe_execute(do_flash_write, page, 100) == PICO_OK) g_dirty = false;
    else g_dirty_since_ms = now_ms;  // retry later
}

bool loaded_from_flash() { return g_loaded; }

}  // namespace params
