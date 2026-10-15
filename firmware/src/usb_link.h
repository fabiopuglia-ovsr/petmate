// USB CDC link carrying the COBS/CRC16 protocol (INTERFACES.md §2).
// All TX is non-blocking: if the host is not draining the endpoint, frames
// are dropped (and counted) rather than stalling the control loop.
#pragma once

#include <cstddef>
#include <cstdint>

#include "protocol.h"

namespace usb_link {

void init();
// Services TinyUSB and decodes received bytes. Calls `on_frame` for each
// valid frame. Must be called frequently (every main-loop iteration).
void poll(void (*on_frame)(const proto::Frame&));

bool connected();  // host has the port open (DTR asserted)

// Sends a frame; `seq` < 0 uses the internal TX sequence counter.
bool send(proto::MsgType type, const uint8_t* payload, size_t len, int seq = -1);

template <typename T>
bool send_msg(proto::MsgType type, const T& msg, int seq = -1) {
    uint8_t pl[T::SIZE];
    if (msg.encode(pl, sizeof pl) != T::SIZE) return false;
    return send(type, pl, T::SIZE, seq);
}

// printf-style LOG message (truncated to 120 bytes).
void logf(const char* fmt, ...) __attribute__((format(printf, 1, 2)));

struct Stats { uint32_t rx_frames, rx_crc_err, rx_framing_err, tx_frames, tx_dropped; };
Stats stats();

}  // namespace usb_link
