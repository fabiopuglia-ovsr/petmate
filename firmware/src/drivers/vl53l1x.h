// Minimal register-level driver for the ST VL53L1X time-of-flight sensor.
//
// Scope and honesty note:
//  - The init sequence reproduces ST's Ultra Lite Driver (ULD, STSW-IMG009)
//    VL53L1X_SensorInit(): it writes ST's default configuration blob to
//    registers 0x2D..0x87, runs one dummy ranging cycle, then sets the VHV
//    loop bound. The blob is copied from ST's BSD-3-Clause ULD (see
//    vl53l1x.cpp) - it is not documented register-by-register by ST.
//  - Only the subset of ULD functionality used here is implemented: distance
//    mode short/long, timing budgets 20/33/50/100 ms, continuous ranging,
//    polling for data ready, range status mapping and I2C address change.
//  - Not implemented (use the full ULD if needed): ROI, offset/crosstalk
//    calibration, thresholds/interrupt windows, signal/sigma tuning.
#pragma once

#include <cstdint>

#include "hardware/i2c.h"

namespace vl53l1x {

enum class DistanceMode : uint8_t { SHORT = 1, LONG = 2 };

class Device {
public:
    Device() = default;
    Device(i2c_inst_t* bus, uint8_t addr) : bus_(bus), addr_(addr) {}

    // Waits (bounded) for firmware boot after XSHUT release.
    bool wait_boot(uint32_t timeout_ms);
    bool check_model_id();                    // reg 0x010F == 0xEACC
    bool set_address(uint8_t new_addr);       // 7-bit address
    bool sensor_init();                       // ULD SensorInit equivalent
    bool set_distance_mode(DistanceMode m);
    bool set_timing_budget_ms(uint16_t ms);   // 20, 33, 50, 100
    bool set_inter_measurement_ms(uint16_t ms);
    bool start_ranging();
    bool stop_ranging();
    bool data_ready(bool& ready);
    // Reads distance and status and clears the interrupt. `status` uses the
    // ULD convention: 0 = valid, others = warnings/errors.
    bool read(uint16_t& mm, uint8_t& status);

    uint8_t address() const { return addr_; }

private:
    bool wr8(uint16_t reg, uint8_t v);
    bool wr16(uint16_t reg, uint16_t v);
    bool wr32(uint16_t reg, uint32_t v);
    bool rd8(uint16_t reg, uint8_t& v);
    bool rd16(uint16_t reg, uint16_t& v);
    bool interrupt_polarity(uint8_t& pol);

    i2c_inst_t* bus_ = nullptr;
    uint8_t addr_ = 0x29;
    DistanceMode mode_ = DistanceMode::LONG;
    uint16_t budget_ms_ = 100;
};

}  // namespace vl53l1x
