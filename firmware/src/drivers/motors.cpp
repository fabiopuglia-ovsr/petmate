#include "motors.h"

#include <cmath>

#include "config.h"
#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "hardware/pwm.h"

namespace motors {
namespace {

struct Hw { unsigned pwm, dir; bool invert; };
constexpr Hw HW[2] = {
    {cfg::PIN_MOT_L_PWM, cfg::PIN_MOT_L_DIR, cfg::MOTOR_L_INVERT},
    {cfg::PIN_MOT_R_PWM, cfg::PIN_MOT_R_DIR, cfg::MOTOR_R_INVERT},
};
uint16_t g_top = 0;
float g_duty[2] = {0.f, 0.f};

}  // namespace

void init() {
    // nFAULT: open-drain wired-OR, active low.
    gpio_init(cfg::PIN_DRV_NFAULT);
    gpio_set_dir(cfg::PIN_DRV_NFAULT, GPIO_IN);
    gpio_pull_up(cfg::PIN_DRV_NFAULT);

    // Phase-correct is not needed; plain edge-aligned PWM at 20 kHz with the
    // full sys clock gives ~7500 steps of resolution at 150 MHz.
    g_top = static_cast<uint16_t>(clock_get_hz(clk_sys) / cfg::MOTOR_PWM_HZ - 1);
    for (const Hw& h : HW) {
        gpio_init(h.dir);
        gpio_set_dir(h.dir, GPIO_OUT);
        gpio_put(h.dir, 0);
        gpio_set_function(h.pwm, GPIO_FUNC_PWM);
        const unsigned slice = pwm_gpio_to_slice_num(h.pwm);
        pwm_config c = pwm_get_default_config();
        pwm_config_set_clkdiv(&c, 1.f);
        pwm_config_set_wrap(&c, g_top);
        pwm_init(slice, &c, false);
        pwm_set_gpio_level(h.pwm, 0);
        pwm_set_enabled(slice, true);
    }
}

void set(Side s, float d) {
    if (!(d == d)) d = 0.f;  // NaN guard
    if (d > 1.f) d = 1.f;
    if (d < -1.f) d = -1.f;
    g_duty[s] = d;
    const Hw& h = HW[s];
    const bool fwd = (d >= 0.f) != h.invert;
    gpio_put(h.dir, fwd ? 1 : 0);
    pwm_set_gpio_level(h.pwm, static_cast<uint16_t>(std::fabs(d) * (g_top + 1)));
}

void coast() {
    for (int s = 0; s < 2; ++s) {
        g_duty[s] = 0.f;
        pwm_set_gpio_level(HW[s].pwm, 0);
    }
}

float duty(Side s) { return g_duty[s]; }

bool fault_active() { return !gpio_get(cfg::PIN_DRV_NFAULT); }

}  // namespace motors
