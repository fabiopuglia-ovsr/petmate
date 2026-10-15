// Minimal VL53L1X driver - see vl53l1x.h for scope.
//
// The default configuration blob and the timing-budget / distance-mode
// register values below are taken from STMicroelectronics' VL53L1X Ultra Lite
// Driver, distributed under the following license:
//
//   COPYRIGHT(c) 2018 STMicroelectronics
//   Redistribution and use in source and binary forms, with or without
//   modification, are permitted provided that the following conditions are met:
//   1. Redistributions of source code must retain the above copyright notice,
//      this list of conditions and the following disclaimer.
//   2. Redistributions in binary form must reproduce the above copyright
//      notice, this list of conditions and the following disclaimer in the
//      documentation and/or other materials provided with the distribution.
//   3. Neither the name of STMicroelectronics nor the names of its contributors
//      may be used to endorse or promote products derived from this software
//      without specific prior written permission.
//   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
//   AND ANY EXPRESS OR IMPLIED WARRANTIES ARE DISCLAIMED.
#include "vl53l1x.h"

#include "i2c_bus.h"
#include "pico/time.h"

namespace vl53l1x {
namespace {

constexpr uint16_t REG_I2C_SLAVE_DEVICE_ADDRESS = 0x0001;
constexpr uint16_t REG_VHV_TIMEOUT_MACROP_LOOP_BOUND = 0x0008;
constexpr uint16_t REG_GPIO_HV_MUX_CTRL = 0x0030;
constexpr uint16_t REG_GPIO_TIO_HV_STATUS = 0x0031;
constexpr uint16_t REG_PHASECAL_TIMEOUT_MACROP = 0x004B;
constexpr uint16_t REG_RANGE_TIMEOUT_MACROP_A_HI = 0x005E;
constexpr uint16_t REG_RANGE_VCSEL_PERIOD_A = 0x0060;
constexpr uint16_t REG_RANGE_TIMEOUT_MACROP_B_HI = 0x0061;
constexpr uint16_t REG_RANGE_VCSEL_PERIOD_B = 0x0063;
constexpr uint16_t REG_RANGE_VALID_PHASE_HIGH = 0x0069;
constexpr uint16_t REG_INTERMEASUREMENT_PERIOD = 0x006C;
constexpr uint16_t REG_SD_CONFIG_WOI_SD0 = 0x0078;
constexpr uint16_t REG_SD_CONFIG_INITIAL_PHASE_SD0 = 0x007A;
constexpr uint16_t REG_SYSTEM_INTERRUPT_CLEAR = 0x0086;
constexpr uint16_t REG_SYSTEM_MODE_START = 0x0087;
constexpr uint16_t REG_RESULT_RANGE_STATUS = 0x0089;
constexpr uint16_t REG_RESULT_RANGE_MM_SD0 = 0x0096;
constexpr uint16_t REG_RESULT_OSC_CALIBRATE_VAL = 0x00DE;
constexpr uint16_t REG_FIRMWARE_SYSTEM_STATUS = 0x00E5;
constexpr uint16_t REG_IDENTIFICATION_MODEL_ID = 0x010F;

// ST ULD VL51L1X_DEFAULT_CONFIGURATION, registers 0x2D..0x87 (91 bytes).
constexpr uint8_t DEFAULT_CONFIG[] = {
    0x00, 0x01, 0x01, 0x01, 0x02, 0x00, 0x02, 0x08, 0x00, 0x08, 0x10, 0x01, 0x01, 0x00, 0x00, 0x00, // 0x2D-0x3C
    0x00, 0xFF, 0x00, 0x0F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x20, 0x0B, 0x00, 0x00, 0x02, 0x0A, 0x21, // 0x3D-0x4C
    0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0xC8, 0x00, 0x00, 0x38, 0xFF, 0x01, 0x00, 0x08, 0x00, // 0x4D-0x5C
    0x00, 0x01, 0xDB, 0x0F, 0x01, 0xF1, 0x0D, 0x01, 0x68, 0x00, 0x80, 0x08, 0xB8, 0x00, 0x00, 0x00, // 0x5D-0x6C
    0x00, 0x0F, 0x89, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x0F, 0x0D, 0x0E, 0x0E, 0x00, // 0x6D-0x7C
    0x00, 0x02, 0xC7, 0xFF, 0x9B, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00,                               // 0x7D-0x87
};
static_assert(sizeof DEFAULT_CONFIG == 0x87 - 0x2D + 1, "VL53L1X default config size");
// Bytes 0x2E/0x2F = 0x01: I2C and GPIO pads referenced to AVDD (2.8 V), as
// on typical breakout boards. Set to 0x00 only if the pads are pulled to 1.8 V.

// Timing budget register values {budget_ms, A_HI, B_HI} from ST ULD.
struct Tb { uint16_t ms, a, b; };
constexpr Tb TB_SHORT[] = {{20, 0x0051, 0x006E}, {33, 0x00D6, 0x006E}, {50, 0x01AE, 0x01E8}, {100, 0x02E1, 0x0388}};
constexpr Tb TB_LONG[]  = {{20, 0x001E, 0x0022}, {33, 0x0060, 0x006E}, {50, 0x00AD, 0x00C6}, {100, 0x01CC, 0x01EA}};

}  // namespace

bool Device::wr8(uint16_t reg, uint8_t v) { return i2cb::write_reg16(bus_, addr_, reg, &v, 1); }
bool Device::wr16(uint16_t reg, uint16_t v) {
    uint8_t b[2] = {static_cast<uint8_t>(v >> 8), static_cast<uint8_t>(v)};
    return i2cb::write_reg16(bus_, addr_, reg, b, 2);
}
bool Device::wr32(uint16_t reg, uint32_t v) {
    uint8_t b[4] = {static_cast<uint8_t>(v >> 24), static_cast<uint8_t>(v >> 16), static_cast<uint8_t>(v >> 8), static_cast<uint8_t>(v)};
    return i2cb::write_reg16(bus_, addr_, reg, b, 4);
}
bool Device::rd8(uint16_t reg, uint8_t& v) { return i2cb::read_reg16(bus_, addr_, reg, &v, 1); }
bool Device::rd16(uint16_t reg, uint16_t& v) {
    uint8_t b[2];
    if (!i2cb::read_reg16(bus_, addr_, reg, b, 2)) return false;
    v = static_cast<uint16_t>((b[0] << 8) | b[1]);
    return true;
}

bool Device::wait_boot(uint32_t timeout_ms) {
    const absolute_time_t until = make_timeout_time_ms(timeout_ms);
    while (absolute_time_diff_us(get_absolute_time(), until) > 0) {
        uint8_t st = 0;
        if (rd8(REG_FIRMWARE_SYSTEM_STATUS, st) && (st & 0x01)) return true;
        sleep_ms(2);
    }
    return false;
}

bool Device::check_model_id() {
    uint16_t id = 0;
    return rd16(REG_IDENTIFICATION_MODEL_ID, id) && id == 0xEACC;
}

bool Device::set_address(uint8_t new_addr) {
    if (!wr8(REG_I2C_SLAVE_DEVICE_ADDRESS, new_addr & 0x7F)) return false;
    addr_ = new_addr;
    sleep_ms(1);
    return true;
}

bool Device::interrupt_polarity(uint8_t& pol) {
    uint8_t t = 0;
    if (!rd8(REG_GPIO_HV_MUX_CTRL, t)) return false;
    pol = static_cast<uint8_t>(!((t & 0x10) >> 4));
    return true;
}

bool Device::sensor_init() {
    if (!i2cb::write_reg16(bus_, addr_, 0x002D, DEFAULT_CONFIG, sizeof DEFAULT_CONFIG)) return false;
    // Dummy measurement (ULD does the same) to settle the VHV calibration.
    if (!start_ranging()) return false;
    bool ready = false;
    for (int i = 0; i < 150 && !ready; ++i) {
        if (!data_ready(ready)) return false;
        if (!ready) sleep_ms(1);
    }
    if (!ready) return false;
    if (!wr8(REG_SYSTEM_INTERRUPT_CLEAR, 0x01) || !stop_ranging()) return false;
    if (!wr8(REG_VHV_TIMEOUT_MACROP_LOOP_BOUND, 0x09)) return false;  // two bounds VHV
    if (!wr8(0x000B, 0x00)) return false;  // start VHV from previous temperature
    mode_ = DistanceMode::LONG;            // blob default
    budget_ms_ = 100;
    return true;
}

bool Device::set_distance_mode(DistanceMode m) {
    bool ok;
    if (m == DistanceMode::SHORT) {
        ok = wr8(REG_PHASECAL_TIMEOUT_MACROP, 0x14) && wr8(REG_RANGE_VCSEL_PERIOD_A, 0x07) &&
             wr8(REG_RANGE_VCSEL_PERIOD_B, 0x05) && wr8(REG_RANGE_VALID_PHASE_HIGH, 0x38) &&
             wr16(REG_SD_CONFIG_WOI_SD0, 0x0705) && wr16(REG_SD_CONFIG_INITIAL_PHASE_SD0, 0x0606);
    } else {
        ok = wr8(REG_PHASECAL_TIMEOUT_MACROP, 0x0A) && wr8(REG_RANGE_VCSEL_PERIOD_A, 0x0F) &&
             wr8(REG_RANGE_VCSEL_PERIOD_B, 0x0D) && wr8(REG_RANGE_VALID_PHASE_HIGH, 0xB8) &&
             wr16(REG_SD_CONFIG_WOI_SD0, 0x0F0D) && wr16(REG_SD_CONFIG_INITIAL_PHASE_SD0, 0x0E0E);
    }
    if (!ok) return false;
    mode_ = m;
    return set_timing_budget_ms(budget_ms_);  // budget registers depend on mode
}

bool Device::set_timing_budget_ms(uint16_t ms) {
    const Tb* tbl = (mode_ == DistanceMode::SHORT) ? TB_SHORT : TB_LONG;
    for (int i = 0; i < 4; ++i) {
        if (tbl[i].ms == ms) {
            if (!wr16(REG_RANGE_TIMEOUT_MACROP_A_HI, tbl[i].a) || !wr16(REG_RANGE_TIMEOUT_MACROP_B_HI, tbl[i].b)) return false;
            budget_ms_ = ms;
            return true;
        }
    }
    return false;  // unsupported budget
}

bool Device::set_inter_measurement_ms(uint16_t ms) {
    uint16_t clk = 0;
    if (!rd16(REG_RESULT_OSC_CALIBRATE_VAL, clk)) return false;
    clk &= 0x3FF;
    return wr32(REG_INTERMEASUREMENT_PERIOD, static_cast<uint32_t>(clk * ms * 1.075f));
}

bool Device::start_ranging() {
    return wr8(REG_SYSTEM_INTERRUPT_CLEAR, 0x01) && wr8(REG_SYSTEM_MODE_START, 0x40);
}

bool Device::stop_ranging() { return wr8(REG_SYSTEM_MODE_START, 0x00); }

bool Device::data_ready(bool& ready) {
    uint8_t pol = 0, st = 0;
    if (!interrupt_polarity(pol) || !rd8(REG_GPIO_TIO_HV_STATUS, st)) return false;
    ready = (st & 0x01) == pol;
    return true;
}

bool Device::read(uint16_t& mm, uint8_t& status) {
    uint8_t raw = 0;
    if (!rd8(REG_RESULT_RANGE_STATUS, raw) || !rd16(REG_RESULT_RANGE_MM_SD0, mm)) return false;
    // Map device status to ULD status codes (0 = valid).
    static const uint8_t MAP[24] = {255, 255, 255, 5, 2, 4, 1, 7, 3, 0, 255, 255, 9, 13, 255, 255, 255, 255, 10, 6, 255, 255, 11, 12};
    raw &= 0x1F;
    status = raw < 24 ? MAP[raw] : 255;
    return wr8(REG_SYSTEM_INTERRUPT_CLEAR, 0x01);
}

}  // namespace vl53l1x
