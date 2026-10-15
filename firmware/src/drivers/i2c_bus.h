// Thin I2C helpers with bounded timeouts (never block the control loop
// indefinitely on a stuck bus) and error counting.
#pragma once

#include <cstddef>
#include <cstdint>

#include "hardware/i2c.h"

namespace i2cb {

// Initialises the peripheral and pins; performs a 9-clock bus recovery if a
// slave is holding SDA low after an MCU reset mid-transaction.
void init(i2c_inst_t* bus, unsigned sda, unsigned scl, unsigned baud);

// 8-bit register address devices (ICM-42688, INA226, ADS1115, PCA9685).
bool write_reg8(i2c_inst_t* bus, uint8_t addr, uint8_t reg, const uint8_t* data, size_t n);
bool read_reg8(i2c_inst_t* bus, uint8_t addr, uint8_t reg, uint8_t* data, size_t n);
bool write_u8(i2c_inst_t* bus, uint8_t addr, uint8_t reg, uint8_t v);
// Big-endian 16-bit register value (INA226, ADS1115).
bool write_be16(i2c_inst_t* bus, uint8_t addr, uint8_t reg, uint16_t v);
bool read_be16(i2c_inst_t* bus, uint8_t addr, uint8_t reg, uint16_t& v);

// 16-bit register address devices (VL53L1X).
bool write_reg16(i2c_inst_t* bus, uint8_t addr, uint16_t reg, const uint8_t* data, size_t n);
bool read_reg16(i2c_inst_t* bus, uint8_t addr, uint16_t reg, uint8_t* data, size_t n);

// Returns true if a device ACKs at `addr`.
bool probe(i2c_inst_t* bus, uint8_t addr);

uint32_t error_count(i2c_inst_t* bus);

}  // namespace i2cb
