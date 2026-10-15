#include "leds.h"

#include <cmath>

#include "config.h"
#include "control.h"
#include "hardware/pio.h"
#include "power.h"
#include "safety.h"
#include "ws2812.pio.h"

namespace leds {
namespace {

PIO const g_pio = pio1;
int g_sm = -1;
uint8_t g_mode = OFF, g_r = 0, g_g = 0, g_b = 0;
uint32_t g_frame[cfg::LED_COUNT];
constexpr float MAX_BRIGHTNESS = 0.5f;  // limits 5 V current (26 LEDs x 60 mA)

uint32_t grb(float r, float g, float b) {
    auto c = [](float v) { return static_cast<uint32_t>(ctl::clampf(v * MAX_BRIGHTNESS, 0.f, 255.f)); };
    return (c(g) << 24) | (c(r) << 16) | (c(b) << 8);  // MSB-first, 24 bits used
}

void hsv(float h, float& r, float& g, float& b) {
    h = std::fmod(h, 1.f) * 6.f;
    const float x = 1.f - std::fabs(std::fmod(h, 2.f) - 1.f);
    r = g = b = 0.f;
    if (h < 1) { r = 1; g = x; } else if (h < 2) { r = x; g = 1; } else if (h < 3) { g = 1; b = x; }
    else if (h < 4) { g = x; b = 1; } else if (h < 5) { r = x; b = 1; } else { r = 1; b = x; }
    r *= 255.f; g *= 255.f; b *= 255.f;
}

}  // namespace

bool init() {
    if (!pio_can_add_program(g_pio, &ws2812_program)) return false;
    const uint offset = pio_add_program(g_pio, &ws2812_program);
    g_sm = pio_claim_unused_sm(g_pio, false);
    if (g_sm < 0) return false;
    ws2812_program_init(g_pio, g_sm, offset, cfg::PIN_WS2812, 800000.f, false);
    for (auto& px : g_frame) px = 0;
    return true;
}

void set_mode(uint8_t mode, uint8_t r, uint8_t g, uint8_t b) {
    g_mode = mode <= static_cast<uint8_t>(CHASE) ? mode : static_cast<uint8_t>(OFF);
    g_r = r; g_g = g; g_b = b;
}

void render(uint32_t now_ms) {
    if (g_sm < 0) return;
    const float t = now_ms / 1000.f;
    const unsigned N = cfg::LED_RING_COUNT;

    // Safety overlays (firmware-owned, override host mode on the ring).
    if (safety::estop() || safety::motor_fault()) {
        const bool on = (now_ms / 250) % 2;  // 2 Hz red blink
        for (unsigned i = 0; i < cfg::LED_COUNT; ++i) g_frame[i] = on ? grb(255, 0, 0) : 0;
    } else {
        for (unsigned i = 0; i < N; ++i) {
            float r = 0, g = 0, b = 0;
            switch (g_mode) {
                case SOLID: r = g_r; g = g_g; b = g_b; break;
                case BREATHE: {
                    const float k = 0.5f - 0.5f * std::cos(2.f * ctl::PI_F * t / 3.f);  // 3 s period
                    r = g_r * k; g = g_g * k; b = g_b * k;
                    break;
                }
                case RAINBOW: hsv(static_cast<float>(i) / N + t * 0.2f, r, g, b); break;
                case CHASE: {
                    const float head = std::fmod(t * 12.f, static_cast<float>(N));  // 12 LED/s
                    float dist = head - i;
                    if (dist < 0) dist += N;
                    const float k = dist < 6.f ? 1.f - dist / 6.f : 0.f;           // 6-LED tail
                    r = g_r * k; g = g_g * k; b = g_b * k;
                    break;
                }
                default: break;
            }
            g_frame[i] = grb(r, g, b);
        }
        // Ears: status indicator. Amber blink = low battery, dim green =
        // charging, otherwise follow the host colour.
        const auto& pw = power::status();
        uint32_t ear;
        if (pw.soc_pct <= cfg::SOC_LOW_PCT && !pw.charging) ear = ((now_ms / 500) % 2) ? grb(255, 120, 0) : 0;
        else if (pw.charging) ear = grb(0, 80, 0);
        else ear = g_mode == OFF ? 0 : grb(g_r, g_g, g_b);
        g_frame[N] = g_frame[N + 1] = ear;
    }
    // 26 LEDs * 24 bit at 800 kHz = 0.8 ms; FIFO is 8 deep (joined TX), so
    // non-blocking writes would drop pixels. Skip the frame if the previous
    // one is still being clocked out, otherwise push (the blocking put waits
    // at most ~0.8 ms in total).
    if (!pio_sm_is_tx_fifo_empty(g_pio, g_sm)) return;
    for (unsigned i = 0; i < cfg::LED_COUNT; ++i) pio_sm_put_blocking(g_pio, g_sm, g_frame[i]);
}

}  // namespace leds
