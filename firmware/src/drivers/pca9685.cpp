#include "pca9685.h"

#include <cmath>

#include "config.h"
#include "i2c_bus.h"
#include "pico/time.h"

namespace pca9685 {
namespace {

constexpr uint8_t REG_MODE1 = 0x00, REG_MODE2 = 0x01, REG_LED0_ON_L = 0x06;
constexpr uint8_t REG_ALL_LED_ON_L = 0xFA, REG_PRESCALE = 0xFE;
constexpr uint8_t MODE1_AI = 0x20, MODE1_SLEEP = 0x10, MODE1_RESTART = 0x80;
constexpr uint8_t MODE2_OUTDRV = 0x04;  // totem-pole outputs
constexpr float OSC_HZ = 25e6f;         // internal oscillator
constexpr uint16_t CODE_FULL_ON = 0x1000 | 4096, CODE_FULL_OFF = 0x2000, CODE_UNSET = 0xFFFF;

bool g_ok = false;
uint16_t g_cache[16];

bool write_code(uint8_t ch, uint16_t code) {
    if (ch >= 16) return false;
    if (g_cache[ch] == code) return true;
    uint8_t b[4];
    if (code == CODE_FULL_OFF) {          // OFF_H bit4 = full off (has priority)
        b[0] = 0; b[1] = 0; b[2] = 0; b[3] = 0x10;
    } else if (code == CODE_FULL_ON) {    // ON_H bit4 = full on
        b[0] = 0; b[1] = 0x10; b[2] = 0; b[3] = 0;
    } else {                              // ON at 0, OFF at `code` counts
        b[0] = 0; b[1] = 0; b[2] = static_cast<uint8_t>(code & 0xFF); b[3] = static_cast<uint8_t>(code >> 8);
    }
    if (!i2cb::write_reg8(i2c0, cfg::ADDR_PCA9685, REG_LED0_ON_L + 4 * ch, b, 4)) {
        g_cache[ch] = CODE_UNSET;  // force rewrite next time
        return false;
    }
    g_cache[ch] = code;
    return true;
}

}  // namespace

bool init() {
    for (auto& c : g_cache) c = CODE_UNSET;
    auto* bus = i2c0;
    const uint8_t a = cfg::ADDR_PCA9685;
    // prescale = round(osc / (4096 * f)) - 1 ; 1.5 kHz -> 3 (actual ~1526 Hz)
    const uint8_t prescale = static_cast<uint8_t>(std::lround(OSC_HZ / (4096.f * cfg::PCA_PWM_HZ)) - 1);
    g_ok = i2cb::write_u8(bus, a, REG_MODE1, MODE1_SLEEP | MODE1_AI) &&
           i2cb::write_u8(bus, a, REG_PRESCALE, prescale) &&
           i2cb::write_u8(bus, a, REG_MODE2, MODE2_OUTDRV);
    if (g_ok) {
        // All outputs full-off before waking up: motors coast, ToF in reset,
        // servo rail off.
        const uint8_t all_off[4] = {0, 0, 0, 0x10};
        g_ok = i2cb::write_reg8(bus, a, REG_ALL_LED_ON_L, all_off, 4) &&
               i2cb::write_u8(bus, a, REG_MODE1, MODE1_AI);
        sleep_us(600);  // oscillator start-up (500 us max)
        g_ok = g_ok && i2cb::write_u8(bus, a, REG_MODE1, MODE1_AI | MODE1_RESTART);
    }
    if (g_ok) for (auto& c : g_cache) c = CODE_FULL_OFF;
    return g_ok;
}

bool ok() { return g_ok; }

bool set_duty(uint8_t ch, float duty) {
    if (!g_ok) return false;
    uint16_t code;
    if (!(duty > 0.f)) code = CODE_FULL_OFF;  // also catches NaN
    else if (duty >= 1.f) code = CODE_FULL_ON;
    else {
        code = static_cast<uint16_t>(std::lround(duty * 4096.f));
        if (code == 0) code = CODE_FULL_OFF;
        else if (code >= 4096) code = CODE_FULL_ON;
    }
    return write_code(ch, code);
}

bool hbridge(uint8_t in1, uint8_t in2, float duty) {
    if (duty > 0.f) return set_duty(in2, 0.f) && set_duty(in1, duty);
    if (duty < 0.f) return set_duty(in1, 0.f) && set_duty(in2, -duty);
    return set_duty(in1, 0.f) && set_duty(in2, 0.f);
}

}  // namespace pca9685
