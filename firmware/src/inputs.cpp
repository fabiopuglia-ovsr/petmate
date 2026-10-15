#include "inputs.h"

#include "config.h"
#include "events.h"
#include "hardware/adc.h"
#include "hardware/gpio.h"

namespace inputs {
namespace {

// Generic time-based debouncer.
struct Debounce {
    bool state = false;
    bool raw_prev = false;
    uint32_t since_ms = 0;
    // Returns true when the debounced state changed.
    bool update(bool raw, uint32_t now_ms, uint32_t hold_ms) {
        if (raw != raw_prev) { raw_prev = raw; since_ms = now_ms; }
        if (raw != state && now_ms - since_ms >= hold_ms) { state = raw; return true; }
        return false;
    }
};

Debounce g_bump_f, g_bump_r, g_drop, g_btn, g_dock;
uint32_t g_btn_down_ms = 0;
bool g_provision_sent = false;
uint32_t g_dock_last_sample_ms = 0;
bool g_dock_raw = false;

void init_input(unsigned pin, bool pull_up) {
    gpio_init(pin);
    gpio_set_dir(pin, GPIO_IN);
    if (pull_up) gpio_pull_up(pin);
}

}  // namespace

void init() {
    // NC switches to GND with pull-up: open (pressed/fault/broken wire) = HIGH.
    init_input(cfg::PIN_BUMP_FRONT, true);
    init_input(cfg::PIN_BUMP_REAR, true);
    init_input(cfg::PIN_WHEEL_DROP, true);
    init_input(cfg::PIN_BUTTON, true);
    adc_gpio_init(cfg::PIN_DOCK_ADC);   // adc_init() done in power::init()
    // Safety inputs start asserted until the first debounce window has passed
    // with the real level, so a boot with a pressed bumper is never missed.
    g_bump_f.state = g_bump_r.state = g_drop.state = true;
    g_bump_f.raw_prev = g_bump_r.raw_prev = g_drop.raw_prev = true;
}

void update(uint32_t now_ms) {
    // Safety inputs: short debounce (10 ms) to reject EMI spikes only.
    if (g_bump_f.update(gpio_get(cfg::PIN_BUMP_FRONT), now_ms, 10) && g_bump_f.state)
        events::emit(proto::EventCode::BUMP, proto::bump_bit::FRONT);
    if (g_bump_r.update(gpio_get(cfg::PIN_BUMP_REAR), now_ms, 10) && g_bump_r.state)
        events::emit(proto::EventCode::BUMP, proto::bump_bit::REAR);
    if (g_drop.update(gpio_get(cfg::PIN_WHEEL_DROP), now_ms, 20) && g_drop.state)
        events::emit(proto::EventCode::WHEEL_DROP, 0);

    // Paw button: short (< 2 s) and long (2-5 s) reported on release;
    // provisioning reported as soon as 5 s is reached while still held.
    if (g_btn.update(!gpio_get(cfg::PIN_BUTTON), now_ms, cfg::BTN_DEBOUNCE_MS)) {
        if (g_btn.state) {
            g_btn_down_ms = now_ms;
            g_provision_sent = false;
        } else if (!g_provision_sent) {
            const uint32_t held = now_ms - g_btn_down_ms;
            events::emit(held >= cfg::BTN_LONG_MS ? proto::EventCode::BUTTON_LONG : proto::EventCode::BUTTON_SHORT,
                         static_cast<uint16_t>(held > 65535 ? 65535 : held));
        }
    }
    if (g_btn.state && !g_provision_sent && now_ms - g_btn_down_ms >= cfg::BTN_PROVISION_MS) {
        events::emit(proto::EventCode::BUTTON_PROVISION, 0);
        g_provision_sent = true;
    }

    // Dock contacts on ADC1, sampled at 20 Hz with hysteresis, 200 ms debounce.
    if (now_ms - g_dock_last_sample_ms >= 50) {
        g_dock_last_sample_ms = now_ms;
        adc_select_input(cfg::ADC_CH_DOCK);
        const float v = adc_read() * (3.3f / 4096.f);
        if (v > cfg::DOCK_THRESHOLD_V + cfg::DOCK_HYST_V * 0.5f) g_dock_raw = true;
        else if (v < cfg::DOCK_THRESHOLD_V - cfg::DOCK_HYST_V * 0.5f) g_dock_raw = false;
    }
    if (g_dock.update(g_dock_raw, now_ms, 200))
        events::emit(g_dock.state ? proto::EventCode::DOCK_CONTACT : proto::EventCode::UNDOCK, 0);
}

uint8_t bump_mask() {
    return static_cast<uint8_t>((g_bump_f.state ? proto::bump_bit::FRONT : 0) |
                                (g_bump_r.state ? proto::bump_bit::REAR : 0));
}
bool wheel_drop() { return g_drop.state; }
bool button_pressed() { return g_btn.state; }
bool docked() { return g_dock.state; }

}  // namespace inputs
