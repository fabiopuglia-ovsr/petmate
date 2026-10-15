// Hardware-independent control / estimation math (see control.h).
#include "control.h"

#include <cmath>

namespace ctl {

float wrap_angle(float a) {
    while (a > PI_F) a -= 2.f * PI_F;
    while (a <= -PI_F) a += 2.f * PI_F;
    return a;
}

float slew(float current, float target, float rate, float dt) {
    const float step = rate * dt;
    if (target > current + step) return current + step;
    if (target < current - step) return current - step;
    return target;
}

float Pid::update(float setpoint, float meas, float ff, float dt) {
    if (dt <= 0.f) return clampf(ff, out_min, out_max);
    const float err = setpoint - meas;
    float deriv = 0.f;
    if (primed) deriv = -(meas - prev_meas) / dt;  // derivative on measurement
    prev_meas = meas;
    primed = true;

    const float p = kp * err;
    const float d = kd * deriv;
    const float unsat = ff + p + integ + d;
    // Conditional integration: only integrate if it does not push further into
    // saturation.
    const bool sat_hi = unsat >= out_max && err > 0.f;
    const bool sat_lo = unsat <= out_min && err < 0.f;
    if (!sat_hi && !sat_lo) {
        integ += ki * err * dt;
        integ = clampf(integ, out_min, out_max);
    }
    return clampf(ff + p + integ + d, out_min, out_max);
}

void Odometry::update(float d_left, float d_right, float track_m, float dt) {
    const float ds = 0.5f * (d_right + d_left);
    const float dth = (d_right - d_left) / track_m;
    // Second-order Runge-Kutta (midpoint heading) integration.
    const float th_mid = th + 0.5f * dth;
    x += ds * std::cos(th_mid);
    y += ds * std::sin(th_mid);
    th = wrap_angle(th + dth);
    if (dt > 0.f) {
        // Light first-order filter on the twist: raw tick quantisation at
        // 200 Hz is ~0.86 mm/tick -> 0.17 m/s per tick, too noisy unfiltered.
        constexpr float ALPHA = 0.3f;
        v += ALPHA * (ds / dt - v);
        w += ALPHA * (dth / dt - w);
    }
}

uint8_t soc_from_voltage(float pack_v, float current_a, float r_int_ohm) {
    // Per-cell open-circuit voltage vs SoC for typical NMC 18650/21700 cells
    // at room temperature, light load.
    // TODO(calibration): replace with the curve of the actual cells (C/20 discharge).
    static const float ocv[][2] = {
        {3.00f, 0}, {3.30f, 2}, {3.45f, 5}, {3.55f, 10}, {3.62f, 15}, {3.66f, 20},
        {3.70f, 30}, {3.74f, 40}, {3.79f, 50}, {3.85f, 60}, {3.92f, 70},
        {4.00f, 80}, {4.08f, 90}, {4.15f, 96}, {4.20f, 100},
    };
    constexpr int N = sizeof ocv / sizeof ocv[0];
    // Remove the IR drop: while discharging (I<0) terminal V is below OCV.
    const float v_ocv = pack_v - current_a * r_int_ohm;
    const float cell = v_ocv / 4.0f;
    if (cell <= ocv[0][0]) return 0;
    if (cell >= ocv[N - 1][0]) return 100;
    for (int i = 1; i < N; ++i) {
        if (cell <= ocv[i][0]) {
            const float t = (cell - ocv[i - 1][0]) / (ocv[i][0] - ocv[i - 1][0]);
            const float s = ocv[i - 1][1] + t * (ocv[i][1] - ocv[i - 1][1]);
            return static_cast<uint8_t>(std::lround(s));
        }
    }
    return 100;
}

float sharp_gp2y0a51_mm(float volts) {
    // Typical curve from the GP2Y0A51SK0F datasheet (white paper, Vcc = 5 V).
    // Monotonically decreasing voltage with distance, useful range 20-150 mm.
    // TODO(calibration): fit per-unit against the real floor material.
    static const float tbl[][2] = {  // {volts, mm}
        {2.30f, 15}, {1.95f, 20}, {1.50f, 30}, {1.22f, 40}, {1.02f, 50},
        {0.87f, 60}, {0.77f, 70}, {0.68f, 80}, {0.56f, 100}, {0.47f, 120},
        {0.40f, 150},
    };
    constexpr int N = sizeof tbl / sizeof tbl[0];
    if (volts >= tbl[0][0]) return tbl[0][1];
    if (volts <= tbl[N - 1][0]) return 200.f;  // out of range: no floor seen
    for (int i = 1; i < N; ++i) {
        if (volts >= tbl[i][0]) {
            const float t = (volts - tbl[i][0]) / (tbl[i - 1][0] - tbl[i][0]);
            return tbl[i][1] + t * (tbl[i - 1][1] - tbl[i][1]);
        }
    }
    return 200.f;
}

}  // namespace ctl
