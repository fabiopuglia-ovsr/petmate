// Hobby servo outputs on GP0 (teaser arm) and GP1 (ball gate). Both pins
// share PWM slice 0 (channels A/B), configured for 50 Hz with 1 us resolution.
// The 5 V servo rail is switched by PCA9685 channel 11 (see servo_power()).
#pragma once

#include <cstdint>

namespace servo {

enum Id : uint8_t { TEASER = 0, GATE = 1 };

void init();                          // outputs disabled (no pulses)
void set_us(Id id, uint16_t pulse_us);// clamped to 500..2500 us, enables output
void disable(Id id);                  // stop pulses (servo goes limp)
uint16_t get_us(Id id);

// Servo 5 V rail reference counting: each user requests power with a
// deadline; the rail stays on until every request has expired.
void request_power(uint32_t now_ms, uint32_t hold_ms);
void force_power_off();               // ESTOP
void update(uint32_t now_ms);         // call periodically (>= 10 Hz)
bool powered();

}  // namespace servo
