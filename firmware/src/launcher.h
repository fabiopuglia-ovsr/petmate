// Ball launcher: two flywheels (PCA9685 ch 4/5 and 6/7 -> DRV8833 #2), ball
// gate servo (GP1, closed 1000 us / open 1900 us), IR ball barrier at the
// loader (GP21, LOW = ball present).
//
// Sequence per LAUNCH: spin up (open loop, no speed feedback available) ->
// for each ball: wait for a ball at the gate, open gate until the IR barrier
// clears, close gate, wait for flywheel recovery -> DONE. If no ball arrives
// within a timeout -> EMPTY. Flywheels only run while the robot is not
// translating and no bumper is pressed (spec §2).
//
// Also hosts the intake roller (PCA9685 ch 2/3), which feeds the loader.
#pragma once

#include <cstdint>

#include "protocol.h"

namespace launcher {

void init();
bool launch(uint8_t power_pct, uint8_t count);  // false if busy
void intake(uint8_t mode);                      // 0 off, 1 in, 2 out
void update(uint32_t now_ms);                   // call at 50-100 Hz
void force_stop();                              // safety: flywheels + intake off, gate closed

proto::LaunchState state();
uint8_t balls();          // balls launched in the current/last sequence
bool ball_present();

}  // namespace launcher
