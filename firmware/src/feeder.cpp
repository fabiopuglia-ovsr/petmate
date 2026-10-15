#include "feeder.h"

#include <cmath>

#include "config.h"
#include "drivers/hx711.h"
#include "drivers/pca9685.h"
#include "events.h"
#include "hardware/gpio.h"
#include "params.h"
#include "pico/time.h"
#include "safety.h"
#include "usb_link.h"

namespace feeder {
namespace {

using proto::FeedState;

enum class Phase : uint8_t { IDLE, WAIT_STABLE, RUN, UNJAM, SETTLE };

FeedState g_state = FeedState::IDLE;
Phase g_phase = Phase::IDLE;
float g_target_g = 0.f;
float g_start_hopper_g = 0.f;
bool g_have_start = false;       // start weight captured for this meal
float g_dispensed_g = 0.f;
float g_hopper_g = 0.f;          // filtered hopper weight
bool g_have_weight = false;
uint32_t g_start_ms = 0, g_phase_ms = 0, g_last_status_ms = 0;
uint32_t g_last_hall_ms = 0;
uint8_t g_unjam_tries = 0;
volatile uint32_t g_hall_count = 0;
uint32_t g_hall_seen = 0;
uint32_t g_revs_since_progress = 0;
float g_progress_ref_g = 0.f;
bool g_hopper_low_sent = false;
bool g_tare_pending = false;
int64_t g_tare_acc = 0;
int g_tare_n = 0;
// Weight stability: last N raw readings for "stable" detection.
float g_recent[5] = {0, 0, 0, 0, 0};
unsigned g_recent_i = 0, g_recent_n = 0;

void hall_isr(uint gpio, uint32_t events) {
    (void)gpio; (void)events;
    ++g_hall_count;
}

void auger(float duty) { pca9685::hbridge(cfg::PCA_CH_AUGER_IN1, cfg::PCA_CH_AUGER_IN2, duty); }

void send_status() {
    proto::FeedStatus s;
    s.state = static_cast<uint8_t>(g_state);
    s.dispensed_g = g_dispensed_g;
    s.hopper_g = g_hopper_g;
    usb_link::send_msg(proto::MsgType::FEED_STATUS, s);
}

void finish(FeedState st, uint32_t now_ms) {
    auger(0.f);
    g_state = st;
    g_phase = Phase::IDLE;
    g_last_status_ms = now_ms;
    if (st == FeedState::JAM) events::emit(proto::EventCode::FEEDER_JAM, static_cast<uint16_t>(g_dispensed_g * 10.f));
    send_status();
    usb_link::logf("feed end state=%u dispensed=%.1f g target=%.1f g", static_cast<unsigned>(st), g_dispensed_g, g_target_g);
}

bool weight_stable() {
    if (g_recent_n < 5) return false;
    float lo = g_recent[0], hi = g_recent[0];
    for (float v : g_recent) { lo = v < lo ? v : lo; hi = v > hi ? v : hi; }
    return (hi - lo) < 1.0f;  // grams; TODO(calibration): depends on cell noise
}

void sample_scale(uint32_t now_ms) {
    int32_t raw;
    if (!hx711::poll(raw)) return;
    if (g_tare_pending) {
        g_tare_acc += raw;
        if (++g_tare_n >= 10) {  // ~1 s at 10 SPS
            params::set_tare_offset(static_cast<int32_t>(g_tare_acc / g_tare_n));
            g_tare_pending = false;
            usb_link::logf("scale tared: offset=%ld", static_cast<long>(g_tare_acc / g_tare_n));
        }
        return;
    }
    const auto& p = params::get();
    const float g = static_cast<float>(raw - p.scale_tare_offset) / p.scale_factor;
    g_recent[g_recent_i] = g;
    g_recent_i = (g_recent_i + 1) % 5;
    if (g_recent_n < 5) ++g_recent_n;
    if (!g_have_weight) { g_hopper_g = g; g_have_weight = true; }
    else g_hopper_g += 0.5f * (g - g_hopper_g);  // light IIR at 10 SPS
    (void)now_ms;

    // Hopper-low event with hysteresis (only meaningful when not dispensing,
    // because the auger vibration disturbs the reading).
    if (g_phase == Phase::IDLE && weight_stable()) {
        if (!g_hopper_low_sent && g_hopper_g < cfg::HOPPER_LOW_G) {
            events::emit(proto::EventCode::HOPPER_LOW, static_cast<uint16_t>(g_hopper_g < 0 ? 0 : g_hopper_g));
            g_hopper_low_sent = true;
        } else if (g_hopper_low_sent && g_hopper_g > cfg::HOPPER_LOW_G + 20.f) {
            g_hopper_low_sent = false;
        }
    }
}

}  // namespace

void init() {
    hx711::init();
    gpio_init(cfg::PIN_AUGER_HALL);
    gpio_set_dir(cfg::PIN_AUGER_HALL, GPIO_IN);
    gpio_pull_up(cfg::PIN_AUGER_HALL);  // open-drain Hall switch
    gpio_set_irq_enabled_with_callback(cfg::PIN_AUGER_HALL, GPIO_IRQ_EDGE_FALL, true, hall_isr);
    auger(0.f);
}

bool start(float grams) {
    if (g_phase != Phase::IDLE || g_tare_pending) return false;
    if (!(grams > 0.f) || grams > cfg::FEED_MAX_G) return false;
    g_target_g = grams;
    g_dispensed_g = 0.f;
    g_unjam_tries = 0;
    g_have_start = false;
    g_state = FeedState::DISPENSING;
    g_phase = Phase::WAIT_STABLE;
    g_start_ms = g_phase_ms = 0;  // set on first update
    send_status();
    return true;
}

void abort() {
    if (g_phase == Phase::IDLE) return;
    finish(FeedState::ABORTED, to_ms_since_boot(get_absolute_time()));
}

bool tare() {
    if (g_phase != Phase::IDLE) return false;
    g_tare_pending = true;
    g_tare_acc = 0;
    g_tare_n = 0;
    return true;
}

void force_stop() { auger(0.f); }

void update(uint32_t now_ms) {
    sample_scale(now_ms);
    if (g_phase == Phase::IDLE) return;
    if (g_start_ms == 0) { g_start_ms = now_ms ? now_ms : 1; g_phase_ms = now_ms; }

    if (!hx711::alive(now_ms) && now_ms - g_start_ms > 1000) {
        usb_link::logf("feed: load cell not responding");
        finish(FeedState::ABORTED, now_ms);
        return;
    }
    if (now_ms - g_start_ms > cfg::FEED_TIMEOUT_MS) {
        finish(FeedState::JAM, now_ms);
        return;
    }
    if (safety::actuators_inhibited()) {
        // ESTOP / nFAULT / wheel drop: stop and abort the meal.
        finish(FeedState::ABORTED, now_ms);
        return;
    }

    const bool allowed = safety::auger_allowed(now_ms);
    switch (g_phase) {
        case Phase::WAIT_STABLE:
            auger(0.f);
            if (allowed && weight_stable()) {
                if (!g_have_start) { g_start_hopper_g = g_hopper_g; g_have_start = true; }  // first run only
                if (g_start_hopper_g < 1.0f) { finish(FeedState::HOPPER_EMPTY, now_ms); return; }
                g_progress_ref_g = g_hopper_g;
                g_revs_since_progress = 0;
                g_hall_seen = g_hall_count;
                g_last_hall_ms = now_ms;
                g_phase = Phase::RUN;
                g_phase_ms = now_ms;
            }
            break;

        case Phase::RUN: {
            if (!allowed) {  // robot moved: pause and wait for stable weight again
                auger(0.f);
                g_phase = Phase::WAIT_STABLE;
                g_phase_ms = now_ms;
                break;
            }
            g_dispensed_g = g_start_hopper_g - g_hopper_g;
            if (g_dispensed_g < 0.f) g_dispensed_g = 0.f;
            const float remaining = g_target_g - g_dispensed_g;
            if (remaining <= cfg::FEED_INFLIGHT_G) {
                auger(0.f);
                g_phase = Phase::SETTLE;
                g_phase_ms = now_ms;
                break;
            }
            auger(remaining < cfg::FEED_SLOW_ZONE_G ? cfg::FEED_AUGER_SLOW_DUTY : cfg::FEED_AUGER_DUTY);

            // Rotation monitoring.
            const uint32_t hall = g_hall_count;
            if (hall != g_hall_seen) {
                g_revs_since_progress += hall - g_hall_seen;
                g_hall_seen = hall;
                g_last_hall_ms = now_ms;
                if (g_progress_ref_g - g_hopper_g > 0.5f) {  // weight is decreasing
                    g_progress_ref_g = g_hopper_g;
                    g_revs_since_progress = 0;
                }
                if (g_revs_since_progress >= cfg::FEED_EMPTY_REVS) {
                    finish(FeedState::HOPPER_EMPTY, now_ms);
                    return;
                }
            } else if (now_ms - g_last_hall_ms > cfg::FEED_JAM_NO_HALL_MS) {
                if (g_unjam_tries >= cfg::FEED_MAX_UNJAM_TRIES) { finish(FeedState::JAM, now_ms); return; }
                ++g_unjam_tries;
                usb_link::logf("feed: auger stalled, unjam attempt %u", g_unjam_tries);
                auger(-cfg::FEED_AUGER_DUTY);
                g_phase = Phase::UNJAM;
                g_phase_ms = now_ms;
            }
            break;
        }

        case Phase::UNJAM:
            if (now_ms - g_phase_ms >= cfg::FEED_UNJAM_REVERSE_MS) {
                auger(0.f);
                g_last_hall_ms = now_ms;
                g_phase = Phase::RUN;
                g_phase_ms = now_ms;
            }
            break;

        case Phase::SETTLE:
            // Let falling kibble land and the scale settle, then decide.
            if (now_ms - g_phase_ms >= 800 && weight_stable()) {
                g_dispensed_g = g_start_hopper_g - g_hopper_g;
                if (g_dispensed_g >= g_target_g - 1.0f) {  // TODO(calibration): tolerance
                    finish(FeedState::DONE, now_ms);
                } else {
                    g_phase = Phase::WAIT_STABLE;          // top up
                    g_phase_ms = now_ms;
                }
            } else if (now_ms - g_phase_ms >= 3000) {
                finish(FeedState::DONE, now_ms);           // scale never settled: accept
            }
            break;

        case Phase::IDLE:
            break;
    }

    // 2 Hz FEED_STATUS while dispensing.
    if (g_phase != Phase::IDLE && now_ms - g_last_status_ms >= 500) {
        g_last_status_ms = now_ms;
        send_status();
    }
}

proto::FeedState state() { return g_state; }
float dispensed_g() { return g_dispensed_g; }
float hopper_g() { return g_hopper_g; }
bool busy() { return g_phase != Phase::IDLE || g_tare_pending; }

void report_rejected() {
    if (busy()) return;
    g_state = FeedState::ABORTED;
    g_dispensed_g = 0.f;
    send_status();
}

}  // namespace feeder
