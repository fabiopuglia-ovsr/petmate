#include "safety.h"

#include "cliff.h"
#include "config.h"
#include "drivers/motors.h"
#include "events.h"
#include "inputs.h"

namespace safety {
namespace {

uint32_t g_last_host_ms = 0;
bool g_host_seen = false;
bool g_wd_tripped = true;     // no host at boot: velocity target is zero
bool g_estop = false;
bool g_fault = false;
uint8_t g_cliff = 0;
uint8_t g_prev_cliff = 0;
uint32_t g_fault_since_ms = 0;
drive::Limits g_lim;

}  // namespace

void init(uint32_t now_ms) {
    g_last_host_ms = now_ms;
    g_wd_tripped = true;
}

void host_alive(uint32_t now_ms) {
    g_last_host_ms = now_ms;
    g_host_seen = true;
    g_wd_tripped = false;
}

void set_estop(bool engaged) { g_estop = engaged; }

void update(uint32_t now_ms) {
    // ---- host watchdog
    if (!g_wd_tripped && now_ms - g_last_host_ms > cfg::HOST_WATCHDOG_MS) {
        g_wd_tripped = true;
        drive::set_target(0.f, 0.f);  // ramped deceleration (spec §2)
        if (g_host_seen) events::emit(proto::EventCode::WATCHDOG, 0);
    }

    // ---- driver fault (wired-OR nFAULT), 1 ms glitch filter via 2 samples
    const bool fault_now = motors::fault_active();
    if (fault_now) {
        if (g_fault_since_ms == 0) g_fault_since_ms = now_ms ? now_ms : 1;
        if (!g_fault && now_ms - g_fault_since_ms >= 1) {
            g_fault = true;
            events::emit(proto::EventCode::MOTOR_FAULT, 0);
        }
    } else {
        g_fault_since_ms = 0;
        g_fault = false;
    }

    // ---- cliffs
    g_cliff = cliff::mask();
    const uint8_t new_cliffs = g_cliff & static_cast<uint8_t>(~g_prev_cliff);
    // Only real detections raise an event; the fail-safe all-set mask while
    // the ADC is not (yet) healthy blocks motion silently (LOG at boot).
    if (new_cliffs && cliff::healthy()) events::emit(proto::EventCode::CLIFF, g_cliff);
    g_prev_cliff = g_cliff;

    // ---- directional blocking
    const uint8_t bump = inputs::bump_mask();
    const bool front_block = (g_cliff & (proto::cliff_bit::FL | proto::cliff_bit::FR)) || (bump & proto::bump_bit::FRONT);
    const bool rear_block = (g_cliff & (proto::cliff_bit::RL | proto::cliff_bit::RR)) || (bump & proto::bump_bit::REAR);
    g_lim.allow_fwd = !front_block;
    g_lim.allow_rev = !rear_block;
    g_lim.allow_any = !inputs::wheel_drop();
    g_lim.coast = g_estop || g_fault;
}

drive::Limits drive_limits() { return g_lim; }
bool estop() { return g_estop; }
bool motor_fault() { return g_fault; }
bool watchdog_tripped() { return g_wd_tripped; }
bool actuators_inhibited() { return g_estop || g_fault || inputs::wheel_drop(); }

bool flywheel_allowed() {
    return !actuators_inhibited() && inputs::bump_mask() == 0 && drive::linear_stopped();
}

bool auger_allowed(uint32_t now_ms) {
    return !actuators_inhibited() && drive::stationary_for(cfg::FEED_STATIONARY_MS, now_ms);
}

uint8_t cliff_mask() { return g_cliff; }

uint8_t flags() {
    uint8_t f = 0;
    if (g_estop) f |= proto::flag::ESTOP;
    if (inputs::wheel_drop()) f |= proto::flag::WHEEL_DROP;
    if (g_fault) f |= proto::flag::MOTOR_FAULT;
    if (g_wd_tripped) f |= proto::flag::WATCHDOG_TRIPPED;
    return f;
}

}  // namespace safety
