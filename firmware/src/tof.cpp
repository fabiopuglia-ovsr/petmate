#include "tof.h"

#include "config.h"
#include "drivers/pca9685.h"
#include "drivers/vl53l1x.h"
#include "pico/time.h"
#include "usb_link.h"

namespace tof {
namespace {

vl53l1x::Device g_dev[COUNT];
bool g_ok[COUNT] = {false, false, false};
uint16_t g_mm[COUNT] = {0, 0, 0};
uint32_t g_last_valid_ms[COUNT] = {0, 0, 0};

bool bring_up(unsigned i) {
    // Release XSHUT for this sensor only; it boots at the default address.
    pca9685::set_digital(cfg::PCA_CH_XSHUT[i], true);
    sleep_ms(2);
    vl53l1x::Device d(i2c1, cfg::ADDR_VL53_DEFAULT);
    if (!d.wait_boot(50) || !d.check_model_id()) return false;
    if (!d.set_address(cfg::ADDR_VL53[i])) return false;
    if (!d.sensor_init()) return false;
    // Long mode (up to ~3.6 m indoors), 33 ms budget, 50 ms period -> 20 Hz.
    if (!d.set_distance_mode(vl53l1x::DistanceMode::LONG) || !d.set_timing_budget_ms(33) ||
        !d.set_inter_measurement_ms(50) || !d.start_ranging())
        return false;
    g_dev[i] = d;
    return true;
}

}  // namespace

bool init() {
    if (!pca9685::ok()) {
        usb_link::logf("tof: PCA9685 missing, XSHUT unavailable");
        return false;
    }
    // Hold all sensors in reset, then bring them up one by one.
    for (unsigned i = 0; i < COUNT; ++i) pca9685::set_digital(cfg::PCA_CH_XSHUT[i], false);
    sleep_ms(5);
    bool all = true;
    for (unsigned i = 0; i < COUNT; ++i) {
        g_ok[i] = bring_up(i);
        if (!g_ok[i]) {
            // Keep a failed sensor in reset so it cannot squat on 0x29 and
            // collide with the next sensor being brought up.
            pca9685::set_digital(cfg::PCA_CH_XSHUT[i], false);
            all = false;
        }
    }
    return all;
}

void update(uint32_t now_ms) {
    for (unsigned i = 0; i < COUNT; ++i) {
        if (!g_ok[i]) continue;
        bool ready = false;
        if (!g_dev[i].data_ready(ready) || !ready) continue;
        uint16_t mm = 0;
        uint8_t st = 255;
        if (g_dev[i].read(mm, st) && st == 0) {
            g_mm[i] = mm;
            g_last_valid_ms[i] = now_ms;
        }
    }
    // Invalidate stale readings after 300 ms.
    for (unsigned i = 0; i < COUNT; ++i)
        if (now_ms - g_last_valid_ms[i] > 300) g_mm[i] = 0;
}

uint16_t range_mm(unsigned idx) { return idx < COUNT ? g_mm[idx] : 0; }
bool sensor_ok(unsigned idx) { return idx < COUNT && g_ok[idx]; }

}  // namespace tof
