#include "drive.h"

#include <cmath>

#include "config.h"
#include "control.h"
#include "drivers/encoders.h"
#include "drivers/motors.h"
#include "params.h"

namespace drive {
namespace {

float g_v_target = 0.f, g_w_target = 0.f;   // host command (clamped)
float g_v_ramp = 0.f, g_w_ramp = 0.f;       // after accel limiting
ctl::Pid g_pid[2];
ctl::Odometry g_odom;
int32_t g_enc_prev[2] = {0, 0};
int32_t g_enc[2] = {0, 0};
float g_wheel_v[2] = {0.f, 0.f};            // measured wheel speeds, m/s (filtered)
bool g_first = true;
uint32_t g_moving_ms = 0;                   // last time motion/command was seen

constexpr float STOP_V_EPS = 0.005f;        // m/s
constexpr float STOP_W_EPS = 0.02f;         // rad/s

float ff_duty(float wheel_v) {
    if (std::fabs(wheel_v) < 1e-4f) return 0.f;
    return wheel_v / cfg::FF_V_AT_FULL_DUTY + ctl::signf(wheel_v) * cfg::FF_STATIC_DUTY;
}

}  // namespace

void init() {
    g_first = true;
    for (auto& p : g_pid) { p.out_min = -1.f; p.out_max = 1.f; p.reset(); }
    motors::coast();
}

void set_target(float v, float w) {
    const auto& p = params::get();
    if (!std::isfinite(v)) v = 0.f;
    if (!std::isfinite(w)) w = 0.f;
    g_v_target = ctl::clampf(v, -p.v_max, p.v_max);
    g_w_target = ctl::clampf(w, -p.w_max, p.w_max);
}

void control_step(float dt, const Limits& lim, uint32_t now_ms) {
    const auto& p = params::get();
    const float m_per_tick = 2.f * ctl::PI_F * p.wheel_radius_m / p.ticks_per_rev;

    // ---- measurement / odometry
    g_enc[0] = encoders::count_left();
    g_enc[1] = encoders::count_right();
    if (g_first) { g_enc_prev[0] = g_enc[0]; g_enc_prev[1] = g_enc[1]; g_first = false; }
    float d[2];
    for (int i = 0; i < 2; ++i) {
        // int32 subtraction handles counter wrap-around.
        const int32_t dt_ticks = static_cast<int32_t>(static_cast<uint32_t>(g_enc[i]) - static_cast<uint32_t>(g_enc_prev[i]));
        g_enc_prev[i] = g_enc[i];
        d[i] = dt_ticks * m_per_tick;
        // ~2.5 ms filter time constant: halves tick quantisation noise
        // while adding little phase lag to a ~10 Hz velocity loop.
        g_wheel_v[i] += 0.5f * (d[i] / dt - g_wheel_v[i]);
    }
    g_odom.update(d[0], d[1], p.track_m, dt);

    // ---- safety shaping of the command
    float v_cmd = g_v_target, w_cmd = g_w_target;
    if (!lim.allow_any || lim.coast) { v_cmd = 0.f; w_cmd = 0.f; }
    if (!lim.allow_fwd && v_cmd > 0.f) v_cmd = 0.f;
    if (!lim.allow_rev && v_cmd < 0.f) v_cmd = 0.f;

    // A blocked direction stops *now*: drop the ramped setpoint immediately
    // instead of decelerating into a cliff/obstacle.
    if ((!lim.allow_fwd && g_v_ramp > 0.f) || (!lim.allow_rev && g_v_ramp < 0.f)) g_v_ramp = 0.f;
    if (!lim.allow_any) { g_v_ramp = 0.f; g_w_ramp = 0.f; }

    // ---- ramps
    g_v_ramp = ctl::slew(g_v_ramp, v_cmd, p.a_max, dt);
    g_w_ramp = ctl::slew(g_w_ramp, w_cmd, cfg::ALPHA_MAX, dt);

    // ---- outputs
    if (lim.coast) {
        motors::coast();
        g_v_ramp = g_w_ramp = 0.f;
        for (auto& pid : g_pid) pid.reset();
    } else {
        float target[2];
        ctl::twist_to_wheels(g_v_ramp, g_w_ramp, p.track_m, target[0], target[1]);
        const bool idle = std::fabs(g_v_ramp) < 1e-4f && std::fabs(g_w_ramp) < 1e-4f &&
                          std::fabs(g_wheel_v[0]) < 0.01f && std::fabs(g_wheel_v[1]) < 0.01f;
        for (int i = 0; i < 2; ++i) {
            auto& pid = g_pid[i];
            pid.kp = p.kp; pid.ki = p.ki; pid.kd = p.kd;
            if (idle) {
                // Settled at zero: release the motor (no integrator hum,
                // no current draw while parked).
                pid.reset();
                motors::set(static_cast<motors::Side>(i), 0.f);
            } else {
                const float u = pid.update(target[i], g_wheel_v[i], ff_duty(target[i]), dt);
                motors::set(static_cast<motors::Side>(i), u);
            }
        }
    }

    const bool moving = std::fabs(g_odom.v) > STOP_V_EPS || std::fabs(g_odom.w) > STOP_W_EPS ||
                        std::fabs(g_v_ramp) > 1e-4f || std::fabs(g_w_ramp) > 1e-4f ||
                        std::fabs(g_v_target) > 1e-4f || std::fabs(g_w_target) > 1e-4f;
    if (moving) g_moving_ms = now_ms;
}

void reset_odometry() { g_odom.reset(); }

float x() { return g_odom.x; }
float y() { return g_odom.y; }
float th() { return g_odom.th; }
float v() { return g_odom.v; }
float w() { return g_odom.w; }
int32_t enc_left() { return g_enc[0]; }
int32_t enc_right() { return g_enc[1]; }
float v_ramped() { return g_v_ramp; }

bool stationary_for(uint32_t ms, uint32_t now_ms) { return now_ms - g_moving_ms >= ms; }

bool linear_stopped() {
    return std::fabs(g_v_ramp) < 1e-4f && std::fabs(g_v_target) < 1e-4f && std::fabs(g_odom.v) < 0.01f;
}

}  // namespace drive
