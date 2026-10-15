// Petmate MCU firmware - entry point and cooperative scheduler.
//
// Single-core, super-loop design. Every task is non-blocking and runs from
// `loop()` when its period has elapsed:
//
//   task            rate     notes
//   --------------  -------  ------------------------------------------------
//   usb poll        always   TinyUSB service + RX frame decoding
//   cliff ADC       ~3 ms/ch ADS1115 single-shot mux, self-paced
//   control         200 Hz   inputs, safety, IMU, PID, odometry
//   STATE TX        50 Hz
//   actuators       50 Hz    feeder, launcher/intake, teaser, servo rail
//   LEDs            50 Hz
//   ToF             20 Hz
//   power           10 Hz
//   params save     1 Hz     only when stationary and actuators idle
//
// The RP2350 hardware watchdog (300 ms) is fed once per loop pass, only if the
// control task has run within the last 50 ms - a stalled control loop resets
// the MCU (all outputs then default to off/coast).
#include <cmath>

#include "cliff.h"
#include "config.h"
#include "drive.h"
#include "drivers/encoders.h"
#include "drivers/i2c_bus.h"
#include "drivers/icm42688.h"
#include "drivers/motors.h"
#include "drivers/pca9685.h"
#include "drivers/servo.h"
#include "events.h"
#include "feeder.h"
#include "hardware/watchdog.h"
#include "inputs.h"
#include "launcher.h"
#include "leds.h"
#include "params.h"
#include "pico/stdlib.h"
#include "power.h"
#include "protocol.h"
#include "safety.h"
#include "teaser.h"
#include "tof.h"
#include "usb_link.h"

namespace {

// Periodic task helper based on the 1 MHz system timer (no drift: the next
// deadline advances by exactly one period; if we fall behind by more than one
// period, we resynchronise instead of bursting).
struct Periodic {
    uint64_t period_us;
    uint64_t next_us = 0;
    bool due(uint64_t now) {
        if (now < next_us) return false;
        next_us += period_us;
        if (now >= next_us) next_us = now + period_us;
        return true;
    }
};

Periodic t_control{1000000 / cfg::CONTROL_HZ};
Periodic t_state{1000000 / cfg::STATE_TX_HZ};
Periodic t_actuators{20000};
Periodic t_leds{20000};
Periodic t_tof{50000};
Periodic t_power{100000};
Periodic t_slow{1000000};

icm42688::Sample g_imu;
uint64_t g_last_control_us = 0;
uint64_t g_prev_control_us = 0;
uint32_t g_boot_ms = 0;
uint32_t g_ctrl_overruns = 0;
bool g_watchdog_reboot = false;

inline uint32_t now_ms() { return to_ms_since_boot(get_absolute_time()); }

// ----------------------------------------------------------- RX dispatch --
void log_reject(const char* what, const char* why) { usb_link::logf("%s rejected: %s", what, why); }

void on_frame(const proto::Frame& f) {
    using proto::MsgType;
    const uint32_t t = now_ms();
    const uint8_t* p = f.payload;
    const size_t n = f.payload_len;
    switch (static_cast<MsgType>(f.type)) {
        case MsgType::CMD_VEL: {
            proto::CmdVel m;
            if (!proto::CmdVel::decode(p, n, m)) { log_reject("CMD_VEL", "length"); break; }
            safety::host_alive(t);
            drive::set_target(m.v_mm_s / 1000.f, m.w_mrad_s / 1000.f);
            break;
        }
        case MsgType::PING: {
            if (n != 0) { log_reject("PING", "length"); break; }
            safety::host_alive(t);
            proto::Pong r;
            r.fw_major = cfg::FW_MAJOR; r.fw_minor = cfg::FW_MINOR; r.fw_patch = cfg::FW_PATCH;
            r.uptime_ms = t;
            usb_link::send_msg(MsgType::PONG, r, f.seq);  // echo the request seq
            break;
        }
        case MsgType::FEED: {
            proto::Feed m;
            if (!proto::Feed::decode(p, n, m)) { log_reject("FEED", "length"); break; }
            if (safety::actuators_inhibited()) {
                log_reject("FEED", "safety inhibit");
                feeder::report_rejected();
                break;
            }
            if (!feeder::start(m.grams_x10 / 10.f)) {
                log_reject("FEED", feeder::busy() ? "busy" : "out of range (0.1..60 g)");
                feeder::report_rejected();
            }
            break;
        }
        case MsgType::FEED_ABORT:
            feeder::abort();
            break;
        case MsgType::TARE_SCALE:
            if (!feeder::tare()) log_reject("TARE_SCALE", "feeder busy");
            break;
        case MsgType::LAUNCH: {
            proto::Launch m;
            if (!proto::Launch::decode(p, n, m)) { log_reject("LAUNCH", "length"); break; }
            if (safety::actuators_inhibited()) { log_reject("LAUNCH", "safety inhibit"); break; }
            if (!launcher::launch(m.power_pct, m.count)) log_reject("LAUNCH", "busy");
            break;
        }
        case MsgType::INTAKE: {
            proto::Intake m;
            if (!proto::Intake::decode(p, n, m)) { log_reject("INTAKE", "length"); break; }
            launcher::intake(m.mode);
            break;
        }
        case MsgType::TEASE: {
            proto::Tease m;
            if (!proto::Tease::decode(p, n, m)) { log_reject("TEASE", "length"); break; }
            teaser::start(m.pattern, m.duration_ms);
            break;
        }
        case MsgType::LED: {
            proto::Led m;
            if (!proto::Led::decode(p, n, m)) { log_reject("LED", "length"); break; }
            leds::set_mode(m.mode, m.r, m.g, m.b);
            break;
        }
        case MsgType::ESTOP: {
            proto::Estop m;
            if (!proto::Estop::decode(p, n, m)) { log_reject("ESTOP", "length"); break; }
            safety::set_estop(m.engage != 0);
            if (m.engage) {
                // Act immediately, do not wait for the next control tick.
                motors::coast();
                feeder::force_stop();
                launcher::force_stop();
                teaser::force_stop();
                drive::set_target(0.f, 0.f);
            }
            usb_link::logf("ESTOP %s", m.engage ? "engaged" : "released");
            break;
        }
        case MsgType::SET_PARAM: {
            proto::SetParam m;
            if (!proto::SetParam::decode(p, n, m)) { log_reject("SET_PARAM", "length"); break; }
            const char* why = nullptr;
            if (params::set(m.param_id, m.value, &why))
                usb_link::logf("param %u = %g", m.param_id, static_cast<double>(m.value));
            else
                usb_link::logf("SET_PARAM %u rejected: %s", m.param_id, why ? why : "?");
            break;
        }
        case MsgType::RESET_ODOM:
            drive::reset_odometry();
            break;
        default:
            usb_link::logf("unknown msg type 0x%02x", f.type);
            break;
    }
}

// ------------------------------------------------------------------ tasks --
void task_control(uint64_t now_us) {
    const uint32_t t = static_cast<uint32_t>(now_us / 1000);
    float dt = g_prev_control_us ? (now_us - g_prev_control_us) * 1e-6f : 1.f / cfg::CONTROL_HZ;
    if (dt > 0.02f) { dt = 0.02f; ++g_ctrl_overruns; }  // clamp after stalls
    g_prev_control_us = now_us;

    inputs::update(t);
    safety::update(t);
    if (icm42688::read(g_imu) && drive::stationary_for(1000, t)) icm42688::update_bias(g_imu);
    drive::control_step(dt, safety::drive_limits(), t);
    if (safety::actuators_inhibited()) {
        feeder::force_stop();
        launcher::force_stop();
    }
    g_last_control_us = now_us;
}

void task_state_tx() {
    proto::State s;
    s.t_ms = now_ms();
    s.x = drive::x(); s.y = drive::y(); s.th = drive::th();
    s.v = drive::v(); s.w = drive::w();
    s.enc_l = drive::enc_left(); s.enc_r = drive::enc_right();
    s.ax_mg = g_imu.ax_mg; s.ay_mg = g_imu.ay_mg; s.az_mg = g_imu.az_mg;
    s.gx_cdps = g_imu.gx_cdps; s.gy_cdps = g_imu.gy_cdps; s.gz_cdps = g_imu.gz_cdps;
    s.cliff_mask = safety::cliff_mask();
    s.bump_mask = inputs::bump_mask();
    const auto& pw = power::status();
    s.flags = safety::flags();
    if (pw.charging) s.flags |= proto::flag::CHARGING;
    if (inputs::docked()) s.flags |= proto::flag::DOCKED;
    if (inputs::button_pressed()) s.flags |= proto::flag::BUTTON_PRESSED;
    s.batt_mv = pw.batt_mv;
    s.batt_ma = pw.batt_ma;
    s.soc_pct = pw.soc_pct;
    usb_link::send_msg(proto::MsgType::STATE, s);
}

void task_actuators(uint32_t t) {
    feeder::update(t);
    launcher::update(t);
    teaser::update(t);
    servo::update(t);
    if (safety::estop()) servo::force_power_off();
}

void task_slow(uint32_t t) {
    const bool idle = drive::stationary_for(2000, t) && !feeder::busy() &&
                      launcher::state() != proto::LaunchState::SPINNING_UP &&
                      launcher::state() != proto::LaunchState::LAUNCHING && !teaser::active();
    params::save_if_dirty(idle, t);
}

void boot_report() {
    // Sent once the host opens the port, so the information is not lost.
    usb_link::logf("petmate fw %u.%u.%u boot%s, params %s", cfg::FW_MAJOR, cfg::FW_MINOR, cfg::FW_PATCH,
                   g_watchdog_reboot ? " (WATCHDOG RESET)" : "", params::loaded_from_flash() ? "from flash" : "default");
    usb_link::logf("hw: pca9685=%d imu=%d tof=%d%d%d cliff=%d", pca9685::ok(), icm42688::ok(), tof::sensor_ok(0),
                   tof::sensor_ok(1), tof::sensor_ok(2), cliff::healthy());
}

}  // namespace

int main() {
    g_watchdog_reboot = watchdog_caused_reboot();

    // Outputs to a safe state first: motors off before anything else.
    motors::init();
    servo::init();

    usb_link::init();
    params::init();

    i2cb::init(i2c0, cfg::PIN_I2C0_SDA, cfg::PIN_I2C0_SCL, cfg::I2C_BAUD);
    i2cb::init(i2c1, cfg::PIN_I2C1_SDA, cfg::PIN_I2C1_SCL, cfg::I2C_BAUD);
    pca9685::init();  // all channels off: auger/flywheels/intake coast, ToF reset, servo rail off

    encoders::init();
    drive::init();
    inputs::init();
    power::init();
    cliff::init();
    icm42688::init();
    feeder::init();
    launcher::init();
    teaser::init();
    leds::init();

    // ToF bring-up takes ~0.5 s (3 sensors incl. dummy ranging); keep USB
    // alive meanwhile. The hardware watchdog is armed only afterwards.
    tof::init();

    g_boot_ms = now_ms();
    safety::init(g_boot_ms);
    watchdog_enable(cfg::HW_WATCHDOG_MS, true);  // pause on debug

    bool was_connected = false;
    const uint64_t t0 = time_us_64();
    t_control.next_us = t_state.next_us = t_actuators.next_us = t_leds.next_us = t0;
    t_tof.next_us = t_power.next_us = t_slow.next_us = t0;

    while (true) {
        const uint64_t now = time_us_64();
        const uint32_t t = static_cast<uint32_t>(now / 1000);

        usb_link::poll(on_frame);
        cliff::step(t);

        if (t_control.due(now)) task_control(now);
        if (t_state.due(now)) task_state_tx();
        if (t_actuators.due(now)) task_actuators(t);
        if (t_leds.due(now)) leds::render(t);
        if (t_tof.due(now)) tof::update(t);
        if (t_power.due(now)) power::update(t, inputs::docked());
        if (t_slow.due(now)) task_slow(t);

        events::flush();

        const bool conn = usb_link::connected();
        if (conn && !was_connected) boot_report();
        was_connected = conn;

        // Feed the hardware watchdog only while the control task is alive.
        if (time_us_64() - g_last_control_us < 50000) watchdog_update();
    }
}
