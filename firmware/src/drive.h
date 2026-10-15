// Differential drive: 200 Hz wheel velocity PID with feed-forward,
// acceleration ramps, limits and wheel odometry.
#pragma once

#include <cstdint>

namespace drive {

struct Limits {
    // Directional motion permissions from the safety supervisor.
    bool allow_fwd = true;   // v > 0
    bool allow_rev = true;   // v < 0
    bool allow_any = true;   // false -> stop everything (wheel drop)
    bool coast = false;      // true  -> remove drive immediately (ESTOP/nFAULT)
};

void init();
// Host command (body twist, m/s and rad/s). Clamped to the param limits.
void set_target(float v, float w);
// Runs one control cycle. `dt` in seconds (nominally 0.005).
void control_step(float dt, const Limits& lim, uint32_t now_ms);
void reset_odometry();

// Odometry / state
float x();
float y();
float th();
float v();       // measured body linear velocity, m/s
float w();       // measured body angular velocity, rad/s
int32_t enc_left();
int32_t enc_right();
float v_ramped();// current (ramped) linear setpoint
bool stationary_for(uint32_t ms, uint32_t now_ms);  // no motion & no command
bool linear_stopped();                               // v setpoint and v measured ~ 0

}  // namespace drive
