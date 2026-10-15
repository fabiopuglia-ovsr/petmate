// Battery monitoring: INA226 (I2C0 @ 0x40) primary, ADC0 divider backup.
// SoC is estimated from the IR-compensated pack voltage (4S Li-ion curve).
#pragma once

#include <cstdint>

namespace power {

struct Status {
    uint16_t batt_mv = 0;
    int16_t batt_ma = 0;     // + = charging (into battery), - = discharging
    uint8_t soc_pct = 0;
    bool charging = false;
    bool ina_ok = false;     // false -> voltage from ADC backup, current = 0
};

void init();
void update(uint32_t now_ms, bool docked);  // call at 10 Hz
const Status& status();

}  // namespace power
