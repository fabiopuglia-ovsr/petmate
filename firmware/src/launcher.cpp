#include "launcher.h"

#include "config.h"
#include "drivers/pca9685.h"
#include "drivers/servo.h"
#include "hardware/gpio.h"
#include "pico/time.h"
#include "safety.h"
#include "usb_link.h"

namespace launcher {
namespace {

using proto::LaunchState;
enum class Phase : uint8_t { IDLE, SPINUP, WAIT_BALL, GATE_OPEN, RECOVER };

LaunchState g_state = LaunchState::IDLE;
Phase g_phase = Phase::IDLE;
float g_power = 0.f;
uint8_t g_count = 0, g_balls = 0;
uint32_t g_phase_ms = 0;
bool g_ball_left = false;
uint8_t g_intake_mode = 0;
bool g_started = false;

void flywheels(float duty) {
    pca9685::hbridge(cfg::PCA_CH_FLY_L_IN1, cfg::PCA_CH_FLY_L_IN2, duty);
    // TODO(calibration): the right flywheel spins opposite to the left one;
    // fix the sign here if the motor leads are wired the other way.
    pca9685::hbridge(cfg::PCA_CH_FLY_R_IN1, cfg::PCA_CH_FLY_R_IN2, duty);
}

void gate(bool open, uint32_t now_ms) {
    servo::request_power(now_ms, 1500);
    servo::set_us(servo::GATE, open ? cfg::GATE_OPEN_US : cfg::GATE_CLOSED_US);
}

void report() {
    proto::LaunchStatus s;
    s.state = static_cast<uint8_t>(g_state);
    s.balls = g_balls;
    usb_link::send_msg(proto::MsgType::LAUNCH_STATUS, s);
}

void set_state(LaunchState st) {
    if (st != g_state) { g_state = st; report(); }
}

void end(LaunchState st, uint32_t now_ms) {
    flywheels(0.f);
    gate(false, now_ms);
    g_phase = Phase::IDLE;
    set_state(st);
}

void apply_intake() {
    // Roller direction: 1 = pull balls in, 2 = push out. Fixed duty.
    // TODO(calibration): duty and sign for the actual roller motor.
    const float d = g_intake_mode == 1 ? 0.8f : (g_intake_mode == 2 ? -0.8f : 0.f);
    pca9685::hbridge(cfg::PCA_CH_INTAKE_IN1, cfg::PCA_CH_INTAKE_IN2, d);
}

}  // namespace

void init() {
    gpio_init(cfg::PIN_BALL_IR);
    gpio_set_dir(cfg::PIN_BALL_IR, GPIO_IN);
    gpio_pull_up(cfg::PIN_BALL_IR);
    flywheels(0.f);
    apply_intake();
}

bool ball_present() { return !gpio_get(cfg::PIN_BALL_IR); }

bool launch(uint8_t power_pct, uint8_t count) {
    if (g_phase != Phase::IDLE) return false;
    if (power_pct < 20) power_pct = 20;           // spec clamp 20..70
    if (power_pct > 70) power_pct = 70;
    if (count < 1) count = 1;                     // spec range 1..4
    if (count > 4) count = 4;
    g_power = power_pct / 100.f;
    g_count = count;
    g_balls = 0;
    g_phase = Phase::SPINUP;
    g_started = false;
    return true;
}

void intake(uint8_t mode) {
    g_intake_mode = mode <= 2 ? mode : 0;
    if (!safety::actuators_inhibited()) apply_intake();
}

void force_stop() {
    flywheels(0.f);
    g_intake_mode = 0;  // after a safety stop the host must re-command the roller
    pca9685::hbridge(cfg::PCA_CH_INTAKE_IN1, cfg::PCA_CH_INTAKE_IN2, 0.f);
    servo::set_us(servo::GATE, cfg::GATE_CLOSED_US);
    if (g_phase != Phase::IDLE) {
        g_phase = Phase::IDLE;
        set_state(LaunchState::IDLE);
    }
}

void update(uint32_t now_ms) {
    if (safety::actuators_inhibited()) {
        if (g_phase != Phase::IDLE) usb_link::logf("launch aborted by safety");
        force_stop();
        return;
    }
    apply_intake();
    if (g_phase == Phase::IDLE) return;
    if (!g_started) {
        g_started = true;
        g_phase_ms = now_ms;
        gate(false, now_ms);
        set_state(LaunchState::SPINNING_UP);
    }
    // Flywheels need v = 0 and no bumper; if violated mid-sequence, spin
    // down and wait (restart spin-up) rather than fire while moving.
    if (!safety::flywheel_allowed()) {
        flywheels(0.f);
        gate(false, now_ms);
        if (g_phase != Phase::SPINUP) { g_phase = Phase::SPINUP; set_state(LaunchState::SPINNING_UP); }
        g_phase_ms = now_ms;
        return;
    }
    flywheels(g_power);
    servo::request_power(now_ms, 1500);

    switch (g_phase) {
        case Phase::SPINUP:
            if (now_ms - g_phase_ms >= cfg::LAUNCH_SPINUP_MS) {
                g_phase = Phase::WAIT_BALL;
                g_phase_ms = now_ms;
                set_state(LaunchState::LAUNCHING);
            }
            break;
        case Phase::WAIT_BALL:
            if (ball_present()) {
                gate(true, now_ms);
                g_ball_left = false;
                g_phase = Phase::GATE_OPEN;
                g_phase_ms = now_ms;
            } else if (now_ms - g_phase_ms >= cfg::LAUNCH_NO_BALL_MS) {
                end(LaunchState::EMPTY, now_ms);  // no (more) balls in the loader
            }
            break;
        case Phase::GATE_OPEN:
            if (!ball_present()) g_ball_left = true;
            if (g_ball_left || now_ms - g_phase_ms >= cfg::LAUNCH_GATE_OPEN_MAX_MS) {
                gate(false, now_ms);
                if (g_ball_left) {
                    ++g_balls;
                    report();
                }
                g_phase = Phase::RECOVER;
                g_phase_ms = now_ms;
            }
            break;
        case Phase::RECOVER:
            if (now_ms - g_phase_ms >= cfg::LAUNCH_RECOVER_MS) {
                if (g_balls >= g_count) end(LaunchState::DONE, now_ms);
                else { g_phase = Phase::WAIT_BALL; g_phase_ms = now_ms; }
            }
            break;
        case Phase::IDLE:
            break;
    }
}

proto::LaunchState state() { return g_state; }
uint8_t balls() { return g_balls; }

}  // namespace launcher
