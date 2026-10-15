// Runtime-tunable parameters (SET_PARAM, INTERFACES.md §2) with validation
// and optional persistence in the last 4 KiB flash sector.
#pragma once

#include <cstdint>

namespace params {

struct Params {
    float kp, ki, kd;            // wheel velocity PID (duty per m/s)
    float ticks_per_rev;         // encoder ticks per wheel revolution (x4)
    float wheel_radius_m;
    float track_m;
    float v_max, w_max, a_max;   // m/s, rad/s, m/s^2
    float scale_factor;          // HX711 counts per gram (sign = load-cell polarity)
    float cliff_threshold_mm;
    int32_t scale_tare_offset;   // HX711 raw value at empty hopper (TARE_SCALE)
};

void init();                     // defaults, then load from flash if valid
const Params& get();

// Validates and applies. Returns false (unchanged) if the id is unknown or
// the value is out of range / not finite. Hard limits are the spec limits.
bool set(uint8_t id, float value, const char** reason);
void set_tare_offset(int32_t raw);

// Persists to flash if something changed and `safe_now` (robot stationary,
// no actuator active). Flash erase blocks the CPU for ~50 ms.
void save_if_dirty(bool safe_now, uint32_t now_ms);
bool loaded_from_flash();

}  // namespace params
