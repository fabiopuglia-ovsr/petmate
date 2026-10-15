#include "servo.h"

#include "config.h"
#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "hardware/pwm.h"
#include "pca9685.h"

namespace servo {
namespace {

constexpr unsigned PINS[2] = {cfg::PIN_SERVO_TEASER, cfg::PIN_SERVO_GATE};
uint16_t g_us[2] = {0, 0};
uint32_t g_power_until_ms = 0;
bool g_powered = false;

}  // namespace

void init() {
    for (unsigned pin : PINS) gpio_set_function(pin, GPIO_FUNC_PWM);
    const unsigned slice = pwm_gpio_to_slice_num(cfg::PIN_SERVO_TEASER);
    pwm_config c = pwm_get_default_config();
    // 1 MHz counter -> 1 us per count; wrap 20000 -> 50 Hz.
    pwm_config_set_clkdiv(&c, static_cast<float>(clock_get_hz(clk_sys)) / 1e6f);
    pwm_config_set_wrap(&c, 20000 - 1);
    pwm_init(slice, &c, true);
    for (unsigned pin : PINS) pwm_set_gpio_level(pin, 0);
}

void set_us(Id id, uint16_t pulse_us) {
    if (pulse_us < cfg::SERVO_MIN_US) pulse_us = cfg::SERVO_MIN_US;
    if (pulse_us > cfg::SERVO_MAX_US) pulse_us = cfg::SERVO_MAX_US;
    g_us[id] = pulse_us;
    pwm_set_gpio_level(PINS[id], pulse_us);
}

void disable(Id id) {
    g_us[id] = 0;
    pwm_set_gpio_level(PINS[id], 0);
}

uint16_t get_us(Id id) { return g_us[id]; }

void request_power(uint32_t now_ms, uint32_t hold_ms) {
    const uint32_t until = now_ms + hold_ms;
    if (static_cast<int32_t>(until - g_power_until_ms) > 0 || !g_powered) g_power_until_ms = until;
    if (!g_powered) {
        g_powered = pca9685::set_digital(cfg::PCA_CH_SERVO_5V, true);
    }
}

void force_power_off() {
    pca9685::set_digital(cfg::PCA_CH_SERVO_5V, false);
    g_powered = false;
}

void update(uint32_t now_ms) {
    if (g_powered && static_cast<int32_t>(now_ms - g_power_until_ms) >= 0) {
        force_power_off();
    }
}

bool powered() { return g_powered; }

}  // namespace servo
