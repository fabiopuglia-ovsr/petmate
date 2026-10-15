#include "usb_link.h"

#include <cstdarg>
#include <cstdio>

#include "pico/bootrom.h"
#include "tusb.h"

namespace usb_link {
namespace {

proto::FrameDecoder g_dec;
uint8_t g_tx_seq = 0;
uint32_t g_rx_frames = 0, g_tx_frames = 0, g_tx_dropped = 0;

}  // namespace

void init() { tusb_init(); }

bool connected() { return tud_cdc_connected(); }

void poll(void (*on_frame)(const proto::Frame&)) {
    tud_task();
    uint8_t buf[64];
    while (tud_cdc_available()) {
        const uint32_t n = tud_cdc_read(buf, sizeof buf);
        if (n == 0) break;
        for (uint32_t i = 0; i < n; ++i) {
            proto::Frame f;
            if (g_dec.push(buf[i], f) == proto::FrameDecoder::Result::FRAME) {
                ++g_rx_frames;
                if (on_frame) on_frame(f);
            }
        }
    }
}

bool send(proto::MsgType type, const uint8_t* payload, size_t len, int seq) {
    uint8_t wire[proto::MAX_WIRE_FRAME];
    const uint8_t s = seq < 0 ? g_tx_seq++ : static_cast<uint8_t>(seq);
    const size_t n = proto::build_frame(static_cast<uint8_t>(type), s, payload, len, wire, sizeof wire);
    if (n == 0) { ++g_tx_dropped; return false; }
    // Drop whole frames only: never write a partial frame into the FIFO.
    if (!tud_cdc_connected() || tud_cdc_write_available() < n) { ++g_tx_dropped; return false; }
    tud_cdc_write(wire, n);
    tud_cdc_write_flush();
    ++g_tx_frames;
    return true;
}

void logf(const char* fmt, ...) {
    char text[proto::MAX_LOG_TEXT + 1];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if (n > static_cast<int>(proto::MAX_LOG_TEXT)) n = proto::MAX_LOG_TEXT;
    send(proto::MsgType::LOG, reinterpret_cast<const uint8_t*>(text), static_cast<size_t>(n));
}

Stats stats() {
    return {g_rx_frames, g_dec.crc_errors(), g_dec.framing_errors(), g_tx_frames, g_tx_dropped};
}

}  // namespace usb_link

// Opening the port at 1200 baud reboots into the BOOTSEL USB bootloader
// (same convention as the Pico SDK / Arduino), enabling flashing without
// pressing the BOOTSEL button. Normal clients must not use 1200 baud.
extern "C" void tud_cdc_line_coding_cb(uint8_t itf, cdc_line_coding_t const* coding) {
    (void)itf;
    if (coding->bit_rate == 1200) reset_usb_boot(0, 0);
}
