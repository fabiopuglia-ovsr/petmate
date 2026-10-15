// Food dispenser: auger (PCA9685 ch 0/1 -> DRV8833 #1 A), hopper load cell
// (HX711), auger revolution Hall sensor (GP20).
//
// Closed-loop dispensing by weight: the hopper is weighed, the auger runs
// until (hopper_start - hopper_now) reaches the target minus the in-flight
// allowance. The auger may only run while the robot has been stationary for
// > 500 ms (stable weight, spec §2); if the robot moves, the auger pauses and
// resumes once stationary again.
//
// Faults: no Hall pulse for 1.5 s while driven -> reverse briefly to unjam
// (2 attempts) then JAM; several revolutions with no weight loss ->
// HOPPER_EMPTY; overall timeout -> JAM.
#pragma once

#include <cstdint>

#include "protocol.h"

namespace feeder {

void init();
bool start(float grams);          // FEED; false if rejected (busy / invalid)
void abort();                     // FEED_ABORT
bool tare();                      // TARE_SCALE; only when idle
void update(uint32_t now_ms);     // call at 50-100 Hz
void force_stop();                // safety: auger off immediately

proto::FeedState state();
float dispensed_g();
float hopper_g();
bool busy();
// Reports a rejected FEED request (FEED_STATUS state=aborted) when idle, so
// the host always receives an answer. No-op while a meal is in progress.
void report_rejected();

}  // namespace feeder
