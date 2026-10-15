// Hardware-independent control / estimation math used by the firmware.
// Kept free of Pico SDK dependencies so it is unit-tested on the host.
#pragma once

#include <cstdint>

namespace ctl {

constexpr float PI_F = 3.14159265358979323846f;

inline float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
inline float signf(float v) { return v > 0.f ? 1.f : (v < 0.f ? -1.f : 0.f); }
float wrap_angle(float a);  // -> (-pi, pi]

// Slew-rate limiter: moves `current` toward `target` by at most rate*dt.
float slew(float current, float target, float rate, float dt);

// PID with output clamping, conditional-integration anti-windup and
// derivative on measurement (no kick on setpoint changes).
struct Pid {
    float kp = 0.f, ki = 0.f, kd = 0.f;
    float out_min = -1.f, out_max = 1.f;
    float integ = 0.f;
    float prev_meas = 0.f;
    bool  primed = false;
    void reset() { integ = 0.f; prev_meas = 0.f; primed = false; }
    // `ff` is added to the output before clamping (feed-forward term).
    float update(float setpoint, float meas, float ff, float dt);
};

// Differential-drive odometry integrated from encoder tick deltas.
struct Odometry {
    float x = 0.f, y = 0.f, th = 0.f;  // pose in odom frame
    float v = 0.f, w = 0.f;            // body twist, filtered
    void reset() { x = y = th = v = w = 0.f; }
    // d_left/d_right: wheel travel (m) since last call; dt in s.
    void update(float d_left, float d_right, float track_m, float dt);
};

// Convert body twist (v, w) to wheel linear speeds (m/s).
inline void twist_to_wheels(float v, float w, float track, float& vl, float& vr) {
    vl = v - w * track * 0.5f;
    vr = v + w * track * 0.5f;
}

// 4S Li-ion state of charge from pack voltage with IR compensation.
// `current_a`: positive = charging (into pack), negative = discharging.
// Open-circuit voltage is estimated as V - I * R_int before table lookup.
uint8_t soc_from_voltage(float pack_v, float current_a, float r_int_ohm);

// Sharp GP2Y0A51SK0F: output voltage (V) -> distance (mm).
// Returns a large value (>= 150 mm) when the reading is below the sensor's
// useful range (no surface / cliff), and 15 mm when saturated close.
float sharp_gp2y0a51_mm(float volts);

}  // namespace ctl
