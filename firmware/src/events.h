// EVENT (0x84) queue. Events raised by any module are buffered and sent from
// the main loop when the USB TX FIFO has room, so short host stalls do not
// lose safety-relevant events.
#pragma once

#include <cstdint>

#include "protocol.h"

namespace events {

void emit(proto::EventCode code, uint16_t data = 0);
void flush();                 // send as many queued events as possible
uint32_t dropped();

}  // namespace events
