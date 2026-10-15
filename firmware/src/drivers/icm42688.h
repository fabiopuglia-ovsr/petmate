// TDK ICM-42688-P 6-axis IMU on I2C0 @ 0x68.
// Configured for +-4 g / +-500 dps, ODR 1 kHz with on-chip UI filter,
// polled at 200 Hz by the control loop.
#pragma once

#include <cstdint>

namespace icm42688 {

struct Sample {
    int16_t ax_mg = 0, ay_mg = 0, az_mg = 0;        // milli-g
    int16_t gx_cdps = 0, gy_cdps = 0, gz_cdps = 0;  // centi-degrees / s
    float gz_rad_s = 0.f;                           // bias-corrected yaw rate
    bool valid = false;
};

bool init();
bool ok();
bool read(Sample& s);
// Estimates the gyro bias while the robot is stationary (call at 200 Hz while
// stationary; the running estimate converges with a ~2 s time constant).
void update_bias(const Sample& s);

}  // namespace icm42688
