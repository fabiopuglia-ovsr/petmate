// Wheel quadrature encoders decoded by PIO (pico-examples quadrature_encoder
// program; x4 decoding, counts in hardware with no CPU load).
#pragma once

#include <cstdint>

namespace encoders {

bool init();                 // claims 2 state machines on pio0
int32_t count_left();        // signed, positive = forward
int32_t count_right();

}  // namespace encoders
