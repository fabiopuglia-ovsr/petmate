// Petmate serial protocol implementation (see protocol.h).
#include "protocol.h"

namespace proto {

uint16_t crc16_ccitt_false(const uint8_t* data, size_t len, uint16_t crc) {
    for (size_t i = 0; i < len; ++i) {
        crc ^= static_cast<uint16_t>(data[i]) << 8;
        for (int b = 0; b < 8; ++b) {
            crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                                 : static_cast<uint16_t>(crc << 1);
        }
    }
    return crc;
}

size_t cobs_encode(const uint8_t* in, size_t len, uint8_t* out, size_t out_cap) {
    if (out_cap < cobs_max_encoded(len)) return 0;
    size_t code_idx = 0;   // position of the current block's code byte
    size_t o = 1;          // next output position
    uint8_t code = 1;      // distance to next zero (block length + 1)
    for (size_t i = 0; i < len; ++i) {
        if (in[i] == 0) {
            out[code_idx] = code;
            code_idx = o++;
            code = 1;
        } else {
            out[o++] = in[i];
            // Maximum block: 254 data bytes. Only open a new block if more input
            // follows (canonical COBS: no trailing 0x01 after a full block).
            if (++code == 0xFF && i + 1 < len) {
                out[code_idx] = code;
                code_idx = o++;
                code = 1;
            }
        }
    }
    out[code_idx] = code;
    return o;
}

size_t cobs_decode(const uint8_t* in, size_t len, uint8_t* out, size_t out_cap) {
    size_t i = 0, o = 0;
    while (i < len) {
        const uint8_t code = in[i++];
        if (code == 0) return COBS_ERROR;              // delimiter inside block
        for (uint8_t k = 1; k < code; ++k) {
            if (i >= len || o >= out_cap) return COBS_ERROR;
            if (in[i] == 0) return COBS_ERROR;
            out[o++] = in[i++];
        }
        // A code < 0xFF implies a zero, except at the very end of the block.
        if (code != 0xFF && i < len) {
            if (o >= out_cap) return COBS_ERROR;
            out[o++] = 0;
        }
    }
    return o;
}

size_t build_frame(uint8_t type, uint8_t seq, const uint8_t* payload, size_t payload_len,
                   uint8_t* out, size_t out_cap) {
    if (payload_len > MAX_PAYLOAD) return 0;
    uint8_t raw[MAX_RAW_FRAME];
    raw[0] = type;
    raw[1] = seq;
    if (payload_len) std::memcpy(raw + HEADER_SIZE, payload, payload_len);
    const size_t body = HEADER_SIZE + payload_len;
    const uint16_t crc = crc16_ccitt_false(raw, body);
    raw[body] = static_cast<uint8_t>(crc & 0xFF);
    raw[body + 1] = static_cast<uint8_t>(crc >> 8);
    const size_t raw_len = body + CRC_SIZE;
    if (out_cap < 1) return 0;
    const size_t n = cobs_encode(raw, raw_len, out, out_cap - 1);
    if (n == 0) return 0;
    out[n] = 0x00;
    return n + 1;
}

FrameDecoder::Result FrameDecoder::push(uint8_t byte, Frame& out) {
    if (byte != 0x00) {
        if (len_ < sizeof buf_) buf_[len_++] = byte;
        else overflow_ = true;
        return Result::NONE;
    }
    // Delimiter: close the current frame.
    const size_t n = len_;
    const bool overflow = overflow_;
    len_ = 0;
    overflow_ = false;
    if (n == 0) return Result::NONE;  // empty frame (e.g. leading delimiter)
    if (overflow) { ++framing_errors_; return Result::ERROR; }

    uint8_t raw[MAX_RAW_FRAME];
    const size_t dl = cobs_decode(buf_, n, raw, sizeof raw);
    if (dl == COBS_ERROR || dl < HEADER_SIZE + CRC_SIZE) { ++framing_errors_; return Result::ERROR; }
    const size_t body = dl - CRC_SIZE;
    const uint16_t rx_crc = static_cast<uint16_t>(raw[body] | (raw[body + 1] << 8));
    if (crc16_ccitt_false(raw, body) != rx_crc) { ++crc_errors_; return Result::ERROR; }
    out.type = raw[0];
    out.seq = raw[1];
    out.payload_len = body - HEADER_SIZE;
    if (out.payload_len) std::memcpy(out.payload, raw + HEADER_SIZE, out.payload_len);
    return Result::FRAME;
}

}  // namespace proto
