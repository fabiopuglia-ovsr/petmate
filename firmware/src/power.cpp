#include "power.h"

#include <cmath>

#include "config.h"
#include "control.h"
#include "drivers/i2c_bus.h"
#include "events.h"
#include "hardware/adc.h"

namespace power {
namespace {

constexpr uint8_t REG_CONFIG = 0x00, REG_SHUNT_V = 0x01, REG_BUS_V = 0x02, REG_DIE_ID = 0xFF;
// AVG=16 (010), VBUSCT=1.1 ms (100), VSHCT=1.1 ms (100), MODE=shunt+bus continuous (111)
constexpr uint16_t INA_CONFIG = (0b010 << 9) | (0b100 << 6) | (0b100 << 3) | 0b111;
constexpr float SHUNT_LSB_V = 2.5e-6f;
constexpr float BUS_LSB_V = 1.25e-3f;

Status g_s;
bool g_ina_present = false;
uint32_t g_ina_fail = 0;
float g_v_filt = 0.f, g_i_filt = 0.f, g_soc_filt = -1.f;
bool g_low_sent = false, g_crit_sent = false;

bool ina_init() {
    uint16_t id = 0;
    if (!i2cb::read_be16(i2c0, cfg::ADDR_INA226, REG_DIE_ID, id) || id != 0x2260) return false;
    return i2cb::write_be16(i2c0, cfg::ADDR_INA226, REG_CONFIG, INA_CONFIG);
}

float adc_vbat() {
    adc_select_input(cfg::ADC_CH_VBAT);
    uint32_t acc = 0;
    for (int i = 0; i < 8; ++i) acc += adc_read();
    // TODO(calibration): divider tolerance and ADC reference (3.3 V nominal).
    return (acc / 8.f) * (3.3f / 4096.f) * cfg::VBAT_DIVIDER;
}

}  // namespace

void init() {
    adc_init();
    adc_gpio_init(cfg::PIN_VBAT_ADC);
    g_ina_present = ina_init();
}

void update(uint32_t now_ms, bool docked) {
    (void)now_ms;
    float v = 0.f, i = 0.f;
    bool ina_ok = false;
    if (g_ina_present) {
        uint16_t sh = 0, bus = 0;
        if (i2cb::read_be16(i2c0, cfg::ADDR_INA226, REG_SHUNT_V, sh) &&
            i2cb::read_be16(i2c0, cfg::ADDR_INA226, REG_BUS_V, bus)) {
            v = bus * BUS_LSB_V;
            i = static_cast<int16_t>(sh) * SHUNT_LSB_V / cfg::INA226_SHUNT_OHM;
            if (cfg::INA226_INVERT_SIGN) i = -i;
            ina_ok = true;
            g_ina_fail = 0;
        } else if (++g_ina_fail > 10) {
            g_ina_present = ina_init();  // try to recover the device
        }
    } else if ((++g_ina_fail % 50) == 0) {
        g_ina_present = ina_init();      // retry every ~5 s
    }
    if (!ina_ok) { v = adc_vbat(); i = 0.f; }

    // First-order filters (10 Hz update): V ~0.5 s, I ~1 s.
    if (g_v_filt <= 0.f) g_v_filt = v;
    g_v_filt += 0.2f * (v - g_v_filt);
    g_i_filt += 0.1f * (i - g_i_filt);

    const float soc_raw = ctl::soc_from_voltage(g_v_filt, g_i_filt, cfg::PACK_R_INT_OHM);
    // SoC filtered slowly (~10 s) so load transients do not make it jump.
    if (g_soc_filt < 0.f) g_soc_filt = soc_raw;
    g_soc_filt += 0.01f * (soc_raw - g_soc_filt);

    g_s.batt_mv = static_cast<uint16_t>(ctl::clampf(g_v_filt * 1000.f, 0.f, 65535.f));
    g_s.batt_ma = static_cast<int16_t>(ctl::clampf(g_i_filt * 1000.f, -32768.f, 32767.f));
    g_s.soc_pct = static_cast<uint8_t>(ctl::clampf(std::lround(g_soc_filt), 0, 100));
    g_s.ina_ok = ina_ok;
    // Charging: current flowing into the pack. Without INA226, assume docked
    // means charging (best available information).
    g_s.charging = ina_ok ? (g_s.batt_ma > cfg::CHARGE_DETECT_MA) : docked;

    // Low / critical events with hysteresis (re-arm 5 % above threshold).
    if (!g_low_sent && g_s.soc_pct <= cfg::SOC_LOW_PCT && !g_s.charging) {
        events::emit(proto::EventCode::BATTERY_LOW, g_s.soc_pct);
        g_low_sent = true;
    } else if (g_low_sent && g_s.soc_pct > cfg::SOC_LOW_PCT + 5) {
        g_low_sent = false;
    }
    if (!g_crit_sent && g_s.soc_pct <= cfg::SOC_CRITICAL_PCT && !g_s.charging) {
        events::emit(proto::EventCode::BATTERY_CRITICAL, g_s.soc_pct);
        g_crit_sent = true;
    } else if (g_crit_sent && g_s.soc_pct > cfg::SOC_CRITICAL_PCT + 5) {
        g_crit_sent = false;
    }
}

const Status& status() { return g_s; }

}  // namespace power
