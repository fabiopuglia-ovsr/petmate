// HX711 24-bit load-cell ADC, bit-banged on GP10 (SCK) / GP11 (DOUT).
// Channel A, gain 128 (25 clock pulses). Output rate 10 SPS (RATE pin low)
// on the reference board.
#pragma once

#include <cstdint>

namespace hx711 {

void init();
// Non-blocking: returns true and fills `raw` if a conversion was ready.
bool poll(int32_t& raw);
bool alive(uint32_t now_ms);   // a sample arrived within the last 500 ms

}  // namespace hx711
