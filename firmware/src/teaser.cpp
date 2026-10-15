#include "teaser.h"

#include <cmath>

#include "config.h"
#include "control.h"
#include "drivers/servo.h"
#include "pico/rand.h"
#include "safety.h"

namespace teaser {
namespace {

uint8_t g_pattern = 0;
uint16_t g_duration_ms = 0;
uint32_t g_start_ms = 0;
bool g_pending = false;
bool g_active = false;
// Random pattern state
float g_rand_target_us = cfg::SERVO_CENTER_US;
float g_pos_us = cfg::SERVO_CENTER_US;
uint32_t g_next_hop_ms = 0;

// Usable arm travel (TODO(calibration): mechanical end stops of the arm).
constexpr float ARM_MIN_US = 800.f;
constexpr float ARM_MAX_US = 2200.f;
constexpr float ARM_MID_US = 0.5f * (ARM_MIN_US + ARM_MAX_US);
constexpr float ARM_HALF_US = 0.5f * (ARM_MAX_US - ARM_MIN_US);

float rand01() { return static_cast<float>(get_rand_32() & 0xFFFF) / 65535.f; }

}  // namespace

void init() { servo::disable(servo::TEASER); }

void start(uint8_t pattern, uint16_t duration_ms) {
    if (pattern == 0 || pattern > 3 || duration_ms == 0) { force_stop(); return; }
    g_pattern = pattern;
    g_duration_ms = duration_ms;
    g_pending = true;
}

void force_stop() {
    if (g_active || g_pending) servo::set_us(servo::TEASER, static_cast<uint16_t>(ARM_MID_US));
    g_active = false;
    g_pending = false;
    g_pattern = 0;
}

bool active() { return g_active; }

void update(uint32_t now_ms) {
    if (safety::actuators_inhibited()) { force_stop(); return; }
    if (g_pending) {
        g_pending = false;
        g_active = true;
        g_start_ms = now_ms;
        g_pos_us = servo::get_us(servo::TEASER) ? servo::get_us(servo::TEASER) : ARM_MID_US;
        g_next_hop_ms = now_ms;
    }
    if (!g_active) return;
    const uint32_t t = now_ms - g_start_ms;
    if (t >= g_duration_ms) {
        // Park at centre; servo rail switches off after the hold time.
        servo::request_power(now_ms, 600);
        servo::set_us(servo::TEASER, static_cast<uint16_t>(ARM_MID_US));
        g_active = false;
        return;
    }
    servo::request_power(now_ms, 600);
    const float ts = t / 1000.f;
    float us = ARM_MID_US;
    switch (g_pattern) {
        case 1:  // wiggle: fast small oscillation (3 Hz, +-30 % travel)
            us = ARM_MID_US + 0.3f * ARM_HALF_US * std::sin(2.f * ctl::PI_F * 3.f * ts);
            break;
        case 2:  // sweep: slow full-travel sweep (0.4 Hz)
            us = ARM_MID_US + ARM_HALF_US * std::sin(2.f * ctl::PI_F * 0.4f * ts);
            break;
        case 3:  // random: hop to random targets with pauses, rate-limited
            if (static_cast<int32_t>(now_ms - g_next_hop_ms) >= 0) {
                g_rand_target_us = ARM_MIN_US + rand01() * (ARM_MAX_US - ARM_MIN_US);
                g_next_hop_ms = now_ms + 250 + static_cast<uint32_t>(rand01() * 1200.f);
            }
            g_pos_us = ctl::slew(g_pos_us, g_rand_target_us, 4000.f, 0.02f);  // us/s at 50 Hz
            us = g_pos_us;
            break;
        default:
            break;
    }
    servo::set_us(servo::TEASER, static_cast<uint16_t>(ctl::clampf(us, ARM_MIN_US, ARM_MAX_US)));
}

}  // namespace teaser
