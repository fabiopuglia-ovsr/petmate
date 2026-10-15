// 3x VL53L1X (left/centre/right) on I2C1. All sensors power up at 0x29; they
// are brought out of reset one at a time via XSHUT (PCA9685 ch 8/9/10) and
// re-addressed to 0x30/0x31/0x32.
//
// NOTE: INTERFACES.md §2 defines no message carrying ToF ranges, and §2's
// safety rules do not use them. Readings are therefore kept on the MCU for
// diagnostics (LOG on init failure) and for a future protocol revision.
#pragma once

#include <cstdint>

namespace tof {

constexpr unsigned COUNT = 3;

bool init();                 // requires pca9685::init() first
void update(uint32_t now_ms);// call at ~20 Hz
// Last valid range in mm, or 0 if none / invalid status.
uint16_t range_mm(unsigned idx);
bool sensor_ok(unsigned idx);

}  // namespace tof
