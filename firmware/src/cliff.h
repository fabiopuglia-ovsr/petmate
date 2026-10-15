// Cliff sensing: 4x Sharp GP2Y0A51SK0F read by ADS1115 (I2C0 @ 0x48),
// AIN0..3 = FL, FR, RL, RR. A cliff is a measured floor distance above the
// threshold (default 60 mm, SET_PARAM 11).
//
// The ADS1115 is multiplexed in single-shot mode by a non-blocking state
// machine: each step() reads the finished conversion and starts the next one.
#pragma once

#include <cstdint>

namespace cliff {

void init();
void step(uint32_t now_ms);  // call every main-loop pass; self-paced at ~2-3 ms/channel
uint8_t mask();              // bit0 FL, bit1 FR, bit2 RL, bit3 RR (debounced)
float distance_mm(unsigned ch);
bool healthy();              // ADC responding; when false mask() = 0x0F (fail-safe)

}  // namespace cliff
