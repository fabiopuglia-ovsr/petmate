// TinyUSB configuration: single CDC-ACM device interface for the Petmate
// binary protocol. No stdio over USB (debug printf goes to the LOG message).
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#define CFG_TUSB_RHPORT0_MODE (OPT_MODE_DEVICE)
#ifndef CFG_TUSB_OS
#define CFG_TUSB_OS OPT_OS_PICO
#endif
#define CFG_TUD_ENDPOINT0_SIZE 64

#define CFG_TUD_CDC    1
#define CFG_TUD_MSC    0
#define CFG_TUD_HID    0
#define CFG_TUD_MIDI   0
#define CFG_TUD_VENDOR 0

// Generous buffers: one STATE frame is ~57 bytes on the wire; at 50 Hz plus
// events/logs we stay far below full-speed bandwidth, but larger FIFOs absorb
// host scheduling jitter without dropping frames.
#define CFG_TUD_CDC_RX_BUFSIZE 512
#define CFG_TUD_CDC_TX_BUFSIZE 1024
#define CFG_TUD_CDC_EP_BUFSIZE 64

#ifdef __cplusplus
}
#endif
