#include "icm42688.h"

#include <cmath>

#include "config.h"
#include "i2c_bus.h"
#include "pico/time.h"

namespace icm42688 {
namespace {

// Bank 0 registers
constexpr uint8_t REG_DEVICE_CONFIG = 0x11;
constexpr uint8_t REG_TEMP_DATA1    = 0x1D;  // burst: temp(2) accel(6) gyro(6)
constexpr uint8_t REG_PWR_MGMT0     = 0x4E;
constexpr uint8_t REG_GYRO_CONFIG0  = 0x4F;
constexpr uint8_t REG_ACCEL_CONFIG0 = 0x50;
constexpr uint8_t REG_GYRO_ACCEL_CONFIG0 = 0x52;
constexpr uint8_t REG_WHO_AM_I      = 0x75;
constexpr uint8_t REG_BANK_SEL      = 0x76;
constexpr uint8_t WHO_AM_I_VALUE    = 0x47;

// FS_SEL / ODR encodings (datasheet DS-000347 §14)
constexpr uint8_t GYRO_FS_500DPS = 2 << 5;
constexpr uint8_t ACCEL_FS_4G    = 2 << 5;
constexpr uint8_t ODR_1KHZ       = 0x06;
constexpr float ACC_LSB_PER_G    = 8192.f;  // +-4 g
constexpr float GYRO_LSB_PER_DPS = 65.5f;   // +-500 dps

bool g_ok = false;
float g_bias_dps[3] = {0.f, 0.f, 0.f};
bool g_bias_init = false;
constexpr float DEG2RAD = 0.017453292519943295f;

int16_t be16(const uint8_t* p) { return static_cast<int16_t>((p[0] << 8) | p[1]); }
int16_t sat16(float v) {
    if (v > 32767.f) return 32767;
    if (v < -32768.f) return -32768;
    return static_cast<int16_t>(std::lround(v));
}

}  // namespace

bool init() {
    auto* bus = i2c0;
    const uint8_t a = cfg::ADDR_ICM42688;
    g_ok = false;
    i2cb::write_u8(bus, a, REG_BANK_SEL, 0);
    if (!i2cb::write_u8(bus, a, REG_DEVICE_CONFIG, 0x01)) return false;  // soft reset
    sleep_ms(2);
    uint8_t who = 0;
    if (!i2cb::read_reg8(bus, a, REG_WHO_AM_I, &who, 1) || who != WHO_AM_I_VALUE) return false;
    // UI filter: 2nd order defaults, BW = ODR/4 for both sensors (0x11).
    g_ok = i2cb::write_u8(bus, a, REG_GYRO_CONFIG0, GYRO_FS_500DPS | ODR_1KHZ) &&
           i2cb::write_u8(bus, a, REG_ACCEL_CONFIG0, ACCEL_FS_4G | ODR_1KHZ) &&
           i2cb::write_u8(bus, a, REG_GYRO_ACCEL_CONFIG0, 0x11) &&
           i2cb::write_u8(bus, a, REG_PWR_MGMT0, 0x0F);  // gyro + accel low-noise mode
    sleep_ms(1);  // datasheet: no register writes for 200 us after PWR_MGMT0
    return g_ok;
}

bool ok() { return g_ok; }

bool read(Sample& s) {
    s.valid = false;
    if (!g_ok) return false;
    uint8_t b[14];
    if (!i2cb::read_reg8(i2c0, cfg::ADDR_ICM42688, REG_TEMP_DATA1, b, sizeof b)) return false;
    const float ax = be16(b + 2), ay = be16(b + 4), az = be16(b + 6);
    const float gx = be16(b + 8) / GYRO_LSB_PER_DPS - g_bias_dps[0];
    const float gy = be16(b + 10) / GYRO_LSB_PER_DPS - g_bias_dps[1];
    const float gz = be16(b + 12) / GYRO_LSB_PER_DPS - g_bias_dps[2];
    // -32768 is reported when data is not ready / sensor in reset.
    if (be16(b + 2) == -32768) return false;
    // TODO(calibration): axis remap if the IMU is not mounted aligned with
    // base_link (x forward, y left, z up). Assumed aligned here.
    s.ax_mg = sat16(ax * 1000.f / ACC_LSB_PER_G);
    s.ay_mg = sat16(ay * 1000.f / ACC_LSB_PER_G);
    s.az_mg = sat16(az * 1000.f / ACC_LSB_PER_G);
    s.gx_cdps = sat16(gx * 100.f);
    s.gy_cdps = sat16(gy * 100.f);
    s.gz_cdps = sat16(gz * 100.f);
    s.gz_rad_s = gz * DEG2RAD;
    s.valid = true;
    return true;
}

void update_bias(const Sample& s) {
    if (!s.valid) return;
    // Raw (un-biased) rate in dps reconstructed from the corrected sample.
    const float raw[3] = {s.gx_cdps / 100.f + g_bias_dps[0], s.gy_cdps / 100.f + g_bias_dps[1],
                          s.gz_cdps / 100.f + g_bias_dps[2]};
    // Reject samples with obvious motion (> 3 dps after correction).
    if (g_bias_init && (std::fabs(s.gz_cdps) > 300 || std::fabs(s.gx_cdps) > 300 || std::fabs(s.gy_cdps) > 300)) return;
    const float k = g_bias_init ? 0.0025f : 1.f;  // ~2 s time constant at 200 Hz
    for (int i = 0; i < 3; ++i) g_bias_dps[i] += k * (raw[i] - g_bias_dps[i]);
    g_bias_init = true;
}

}  // namespace icm42688
