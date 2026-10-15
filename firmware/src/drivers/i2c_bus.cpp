#include "i2c_bus.h"

#include <cstring>

#include "hardware/gpio.h"
#include "pico/time.h"

namespace i2cb {
namespace {

constexpr size_t MAX_WRITE = 128;
uint32_t g_errors[2] = {0, 0};

// Generous per-transfer deadline: 400 kHz -> ~25 us/byte; allow 4x margin.
absolute_time_t deadline(size_t bytes) { return make_timeout_time_us(500 + 100 * bytes); }

bool count(i2c_inst_t* bus, bool ok) {
    if (!ok) ++g_errors[i2c_hw_index(bus)];
    return ok;
}

void recover(unsigned sda, unsigned scl) {
    gpio_init(sda);
    gpio_init(scl);
    gpio_set_dir(sda, GPIO_IN);
    gpio_pull_up(sda);
    gpio_pull_up(scl);
    gpio_set_dir(scl, GPIO_OUT);
    gpio_put(scl, 1);
    sleep_us(5);
    // Clock out up to 9 bits until the slave releases SDA, then issue STOP.
    for (int i = 0; i < 9 && !gpio_get(sda); ++i) {
        gpio_put(scl, 0); sleep_us(5);
        gpio_put(scl, 1); sleep_us(5);
    }
    gpio_set_dir(sda, GPIO_OUT);
    gpio_put(sda, 0); sleep_us(5);
    gpio_put(scl, 1); sleep_us(5);
    gpio_put(sda, 1); sleep_us(5);
}

}  // namespace

void init(i2c_inst_t* bus, unsigned sda, unsigned scl, unsigned baud) {
    recover(sda, scl);
    i2c_init(bus, baud);
    gpio_set_function(sda, GPIO_FUNC_I2C);
    gpio_set_function(scl, GPIO_FUNC_I2C);
    // Board has external 4.7k pull-ups; internal ones are harmless backup.
    gpio_pull_up(sda);
    gpio_pull_up(scl);
}

bool write_reg8(i2c_inst_t* bus, uint8_t addr, uint8_t reg, const uint8_t* data, size_t n) {
    if (n + 1 > MAX_WRITE) return false;
    uint8_t buf[MAX_WRITE];
    buf[0] = reg;
    if (n) std::memcpy(buf + 1, data, n);
    int r = i2c_write_blocking_until(bus, addr, buf, n + 1, false, deadline(n + 1));
    return count(bus, r == static_cast<int>(n + 1));
}

bool read_reg8(i2c_inst_t* bus, uint8_t addr, uint8_t reg, uint8_t* data, size_t n) {
    int r = i2c_write_blocking_until(bus, addr, &reg, 1, true, deadline(1));
    if (r != 1) return count(bus, false);
    r = i2c_read_blocking_until(bus, addr, data, n, false, deadline(n));
    return count(bus, r == static_cast<int>(n));
}

bool write_u8(i2c_inst_t* bus, uint8_t addr, uint8_t reg, uint8_t v) {
    return write_reg8(bus, addr, reg, &v, 1);
}

bool write_be16(i2c_inst_t* bus, uint8_t addr, uint8_t reg, uint16_t v) {
    uint8_t b[2] = {static_cast<uint8_t>(v >> 8), static_cast<uint8_t>(v & 0xFF)};
    return write_reg8(bus, addr, reg, b, 2);
}

bool read_be16(i2c_inst_t* bus, uint8_t addr, uint8_t reg, uint16_t& v) {
    uint8_t b[2];
    if (!read_reg8(bus, addr, reg, b, 2)) return false;
    v = static_cast<uint16_t>((b[0] << 8) | b[1]);
    return true;
}

bool write_reg16(i2c_inst_t* bus, uint8_t addr, uint16_t reg, const uint8_t* data, size_t n) {
    if (n + 2 > MAX_WRITE) return false;
    uint8_t buf[MAX_WRITE];
    buf[0] = static_cast<uint8_t>(reg >> 8);
    buf[1] = static_cast<uint8_t>(reg & 0xFF);
    if (n) std::memcpy(buf + 2, data, n);
    int r = i2c_write_blocking_until(bus, addr, buf, n + 2, false, deadline(n + 2));
    return count(bus, r == static_cast<int>(n + 2));
}

bool read_reg16(i2c_inst_t* bus, uint8_t addr, uint16_t reg, uint8_t* data, size_t n) {
    uint8_t a[2] = {static_cast<uint8_t>(reg >> 8), static_cast<uint8_t>(reg & 0xFF)};
    int r = i2c_write_blocking_until(bus, addr, a, 2, true, deadline(2));
    if (r != 2) return count(bus, false);
    r = i2c_read_blocking_until(bus, addr, data, n, false, deadline(n));
    return count(bus, r == static_cast<int>(n));
}

bool probe(i2c_inst_t* bus, uint8_t addr) {
    uint8_t dummy;
    return i2c_read_blocking_until(bus, addr, &dummy, 1, false, deadline(1)) == 1;
}

uint32_t error_count(i2c_inst_t* bus) { return g_errors[i2c_hw_index(bus)]; }

}  // namespace i2cb
