// Digital/analog discrete inputs: bumpers, wheel-drop, "paw" button, dock
// contacts. Debounced; button gestures and dock transitions raise EVENTs.
#pragma once

#include <cstdint>

namespace inputs {

void init();
void update(uint32_t now_ms);    // call at 100-200 Hz

uint8_t bump_mask();             // bit0 front, bit1 rear
bool wheel_drop();
bool button_pressed();
bool docked();

}  // namespace inputs
