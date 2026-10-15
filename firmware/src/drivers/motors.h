// DRV8874 wheel motor drivers in PH/EN mode: EN = PWM (20 kHz), PH = direction.
//
// IMPORTANT (hardware limitation, see firmware README "Spec notes"):
// in PH/EN mode with nSLEEP tied high, EN = 0 selects *low-side slow-decay
// brake*, not coast. True coast (outputs Hi-Z) would need nSLEEP or PMODE
// control, which the pinout does not provide. `coast()` therefore removes
// drive (EN = 0) - the closest available behaviour. On nFAULT the DRV8874
// itself disables its outputs (Hi-Z), so a fault does yield real coast.
#pragma once

namespace motors {

enum Side { LEFT = 0, RIGHT = 1 };

void init();
// duty in [-1, 1]; positive = wheel turns to drive the robot forward.
void set(Side s, float duty);
void coast();                 // both EN = 0 (see note above)
float duty(Side s);
bool fault_active();          // nFAULT (GP22) asserted low

}  // namespace motors
