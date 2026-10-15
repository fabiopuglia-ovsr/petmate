// WS2812 chain on GP16 (24-LED ring + 2 ears) driven by PIO + DMA-free FIFO
// writes. Host-controlled modes (LED message) plus firmware overlays for
// safety states (ESTOP, fault, low battery) which take precedence.
#pragma once

#include <cstdint>

namespace leds {

enum Mode : uint8_t { OFF = 0, SOLID = 1, BREATHE = 2, RAINBOW = 3, CHASE = 4 };

bool init();                           // claims one SM on pio1
void set_mode(uint8_t mode, uint8_t r, uint8_t g, uint8_t b);
void render(uint32_t now_ms);          // call at ~50 Hz

}  // namespace leds
