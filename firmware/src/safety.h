// Safety supervisor (INTERFACES.md §2 "Sicurezza lato MCU"), independent of
// the host:
//  - host watchdog: no CMD_VEL/PING for 250 ms -> velocity target 0 (ramped)
//  - front cliff (FL|FR) or front bumper -> block v > 0
//  - rear cliff (RL|RR) or rear bumper   -> block v < 0
//  - wheel drop -> stop all motion (and all actuators, see README)
//  - ESTOP or nFAULT -> wheel motors coast, flywheels and auger stopped
//  - flywheels only if v = 0 and no bumper; auger only if stationary > 500 ms
#pragma once

#include <cstdint>

#include "drive.h"

namespace safety {

void init(uint32_t now_ms);
void host_alive(uint32_t now_ms);     // call on every CMD_VEL / PING
void set_estop(bool engaged);
// Evaluates all inputs; call once per control cycle before drive::control_step.
void update(uint32_t now_ms);

drive::Limits drive_limits();
bool estop();
bool motor_fault();
bool watchdog_tripped();
bool actuators_inhibited();           // ESTOP, nFAULT or wheel drop
bool flywheel_allowed();
bool auger_allowed(uint32_t now_ms);
uint8_t cliff_mask();
uint8_t flags();                      // STATE.flags (without charging/docked/button)

}  // namespace safety
