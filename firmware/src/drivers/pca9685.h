// PCA9685 16-channel PWM expander (I2C0 @ 0x41, 1.5 kHz) driving 2x DRV8833,
// the VL53L1X XSHUT lines and the servo 5 V enable MOSFET.
#pragma once

#include <cstdint>

namespace pca9685 {

bool init();          // all channels full-off on success
bool ok();

// duty in [0, 1]; 0 -> full off, 1 -> full on. Writes are cached: an
// unchanged value does not generate I2C traffic.
bool set_duty(uint8_t ch, float duty);
inline bool set_digital(uint8_t ch, bool on) { return set_duty(ch, on ? 1.f : 0.f); }

// DRV8833 half of a bridge on two channels. duty in [-1, 1]; 0 = coast
// (both inputs low, outputs Hi-Z). Fast-decay drive (one input PWM, other low).
bool hbridge(uint8_t in1, uint8_t in2, float duty);

}  // namespace pca9685
