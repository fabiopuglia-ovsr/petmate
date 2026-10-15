// "Teaser" arm servo on GP0: wiggle / sweep / random play patterns.
#pragma once

#include <cstdint>

namespace teaser {

void init();
void start(uint8_t pattern, uint16_t duration_ms);  // 0 stop, 1 wiggle, 2 sweep, 3 random
void update(uint32_t now_ms);                       // call at 50 Hz
void force_stop();
bool active();

}  // namespace teaser
