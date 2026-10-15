#include "hx711.h"

#include "config.h"
#include "hardware/gpio.h"
#include "hardware/sync.h"
#include "pico/time.h"

namespace hx711 {
namespace {
uint32_t g_last_ms = 0;
bool g_seen = false;
}

void init() {
    gpio_init(cfg::PIN_HX711_SCK);
    gpio_set_dir(cfg::PIN_HX711_SCK, GPIO_OUT);
    gpio_put(cfg::PIN_HX711_SCK, 0);  // SCK high > 60 us would power the chip down
    gpio_init(cfg::PIN_HX711_DOUT);
    gpio_set_dir(cfg::PIN_HX711_DOUT, GPIO_IN);
    gpio_pull_up(cfg::PIN_HX711_DOUT);
}

bool poll(int32_t& raw) {
    // DOUT low = conversion ready.
    if (gpio_get(cfg::PIN_HX711_DOUT)) return false;
    uint32_t v = 0;
    // Interrupts off for ~30 us: an SCK high phase longer than 60 us powers
    // down the HX711 and corrupts the read.
    const uint32_t irq = save_and_disable_interrupts();
    for (int i = 0; i < 24; ++i) {
        gpio_put(cfg::PIN_HX711_SCK, 1);
        busy_wait_us_32(1);
        v = (v << 1) | (gpio_get(cfg::PIN_HX711_DOUT) ? 1u : 0u);
        gpio_put(cfg::PIN_HX711_SCK, 0);
        busy_wait_us_32(1);
    }
    // 25th pulse: next conversion on channel A, gain 128.
    gpio_put(cfg::PIN_HX711_SCK, 1);
    busy_wait_us_32(1);
    gpio_put(cfg::PIN_HX711_SCK, 0);
    restore_interrupts(irq);
    // Sign-extend 24-bit two's complement.
    if (v & 0x800000u) v |= 0xFF000000u;
    raw = static_cast<int32_t>(v);
    g_last_ms = to_ms_since_boot(get_absolute_time());
    g_seen = true;
    return true;
}

bool alive(uint32_t now_ms) { return g_seen && (now_ms - g_last_ms) < 500; }

}  // namespace hx711
