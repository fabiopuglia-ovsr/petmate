#include "cliff.h"

#include "config.h"
#include "control.h"
#include "drivers/i2c_bus.h"
#include "params.h"

namespace cliff {
namespace {

constexpr uint8_t REG_CONV = 0x00, REG_CONFIG = 0x01;
// OS=1 (start), MUX=1xx (AINx vs GND), PGA=001 (+-4.096 V), MODE=1 (single-shot),
// DR=111 (860 SPS), comparator disabled (11).
uint16_t config_for(unsigned ch) {
    return static_cast<uint16_t>((1u << 15) | ((0b100u | ch) << 12) | (0b001u << 9) | (1u << 8) | (0b111u << 5) | 0b11u);
}
constexpr float LSB_V = 4.096f / 32768.f;
constexpr uint8_t DEBOUNCE_N = 3;   // consecutive samples to change state (~30 ms)

unsigned g_ch = 0;
bool g_started = false;
float g_mm[4] = {0, 0, 0, 0};
uint8_t g_cnt[4] = {0, 0, 0, 0};
uint8_t g_mask = 0;
uint32_t g_start_ms = 0;
uint32_t g_last_ok_ms = 0;
bool g_healthy = false;

bool start(unsigned ch) { return i2cb::write_be16(i2c0, cfg::ADDR_ADS1115, REG_CONFIG, config_for(ch)); }

}  // namespace

void init() {
    g_ch = 0;
    g_started = start(0);
    g_start_ms = 0;
}

void step(uint32_t now_ms) {
    // 860 SPS -> 1.16 ms per conversion; a >= 3 ms tick gap (>= 2 ms real)
    // guarantees completion
    // without polling the OS bit (saves one I2C transaction per sample).
    if (g_started && now_ms - g_start_ms < 3) return;
    if (g_started) {
        uint16_t raw = 0;
        if (i2cb::read_be16(i2c0, cfg::ADDR_ADS1115, REG_CONV, raw)) {
            const float v = static_cast<int16_t>(raw) * LSB_V;
            const float mm = ctl::sharp_gp2y0a51_mm(v);
            g_mm[g_ch] = mm;
            const bool is_cliff = mm > params::get().cliff_threshold_mm;
            const bool cur = g_mask & (1u << g_ch);
            if (is_cliff != cur) {
                if (++g_cnt[g_ch] >= DEBOUNCE_N) {
                    g_mask ^= static_cast<uint8_t>(1u << g_ch);
                    g_cnt[g_ch] = 0;
                }
            } else {
                g_cnt[g_ch] = 0;
            }
            if (g_ch == 3) g_last_ok_ms = now_ms;  // full sweep completed
        }
        g_ch = (g_ch + 1) & 3;
    }
    g_started = start(g_ch);
    g_start_ms = now_ms;
    // Healthy if a full 4-channel sweep completed recently.
    g_healthy = g_last_ok_ms != 0 && (now_ms - g_last_ok_ms) < 100;
}

uint8_t mask() { return g_healthy ? g_mask : 0x0F; }
float distance_mm(unsigned ch) { return ch < 4 ? g_mm[ch] : 0.f; }
bool healthy() { return g_healthy; }

}  // namespace cliff
