#include "events.h"

#include "usb_link.h"

namespace events {
namespace {

constexpr unsigned QLEN = 32;  // power of two
proto::Event g_q[QLEN];
unsigned g_head = 0, g_tail = 0;
uint32_t g_dropped = 0;

}  // namespace

void emit(proto::EventCode code, uint16_t data) {
    if (g_head - g_tail >= QLEN) {  // full: drop the oldest
        ++g_tail;
        ++g_dropped;
    }
    proto::Event& e = g_q[g_head % QLEN];
    e.code = static_cast<uint8_t>(code);
    e.data = data;
    ++g_head;
}

void flush() {
    while (g_tail != g_head) {
        if (!usb_link::connected()) {
            // Host absent: keep the most recent events for when it reconnects.
            return;
        }
        if (!usb_link::send_msg(proto::MsgType::EVENT, g_q[g_tail % QLEN])) return;  // TX full, retry later
        ++g_tail;
    }
}

uint32_t dropped() { return g_dropped; }

}  // namespace events
