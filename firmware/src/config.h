// Petmate MCU firmware - board configuration.
// Single source: docs/INTERFACES.md §1 (pinout, I2C map, geometry, limits).
// Any change here MUST be reflected in INTERFACES.md first.
#pragma once

#include <cstdint>

namespace cfg {

// ---------------------------------------------------------------- version --
constexpr uint8_t FW_MAJOR = 0;
constexpr uint8_t FW_MINOR = 1;
constexpr uint8_t FW_PATCH = 0;

// ----------------------------------------------------------------- GPIO ----
constexpr unsigned PIN_SERVO_TEASER = 0;   // PWM 50 Hz, 500-2500 us
constexpr unsigned PIN_SERVO_GATE   = 1;   // PWM 50 Hz, closed 1000 us, open 1900 us
constexpr unsigned PIN_MOT_L_PWM    = 2;   // DRV8874 EN (left)
constexpr unsigned PIN_MOT_L_DIR    = 3;   // DRV8874 PH (left)
constexpr unsigned PIN_MOT_R_PWM    = 4;   // DRV8874 EN (right)
constexpr unsigned PIN_MOT_R_DIR    = 5;   // DRV8874 PH (right)
constexpr unsigned PIN_ENC_L_A      = 6;   // GP6/GP7 left A/B (PIO, consecutive pins)
constexpr unsigned PIN_ENC_R_A      = 8;   // GP8/GP9 right A/B
constexpr unsigned PIN_HX711_SCK    = 10;
constexpr unsigned PIN_HX711_DOUT   = 11;
constexpr unsigned PIN_I2C0_SDA     = 12;
constexpr unsigned PIN_I2C0_SCL     = 13;
constexpr unsigned PIN_I2C1_SDA     = 14;
constexpr unsigned PIN_I2C1_SCL     = 15;
constexpr unsigned PIN_WS2812       = 16;
constexpr unsigned PIN_BUMP_FRONT   = 17;  // NC to GND + pull-up: active = HIGH
constexpr unsigned PIN_BUMP_REAR    = 18;  // NC to GND + pull-up: active = HIGH
constexpr unsigned PIN_WHEEL_DROP   = 19;  // 2x NC in series: active = HIGH
constexpr unsigned PIN_AUGER_HALL   = 20;  // 1 pulse / auger revolution
constexpr unsigned PIN_BALL_IR      = 21;  // LOW = ball present at loader
constexpr unsigned PIN_DRV_NFAULT   = 22;  // wired-OR, active LOW
constexpr unsigned PIN_VBAT_ADC     = 26;  // ADC0, divider 1:6
constexpr unsigned PIN_DOCK_ADC     = 27;  // ADC1, > 1.5 V = docked
constexpr unsigned PIN_BUTTON       = 28;  // active LOW

constexpr unsigned ADC_CH_VBAT = 0;
constexpr unsigned ADC_CH_DOCK = 1;

// ----------------------------------------------------------------- I2C -----
constexpr unsigned I2C_BAUD = 400000;
constexpr uint8_t ADDR_ICM42688 = 0x68;   // I2C0
constexpr uint8_t ADDR_INA226   = 0x40;   // I2C0
constexpr uint8_t ADDR_ADS1115  = 0x48;   // I2C0
constexpr uint8_t ADDR_PCA9685  = 0x41;   // I2C0
constexpr uint8_t ADDR_VL53_DEFAULT = 0x29;            // I2C1, power-on address
constexpr uint8_t ADDR_VL53[3] = {0x30, 0x31, 0x32};   // left, centre, right

// ------------------------------------------------------- PCA9685 channels --
constexpr float   PCA_PWM_HZ = 1500.0f;
constexpr uint8_t PCA_CH_AUGER_IN1  = 0;
constexpr uint8_t PCA_CH_AUGER_IN2  = 1;
constexpr uint8_t PCA_CH_INTAKE_IN1 = 2;
constexpr uint8_t PCA_CH_INTAKE_IN2 = 3;
constexpr uint8_t PCA_CH_FLY_L_IN1  = 4;
constexpr uint8_t PCA_CH_FLY_L_IN2  = 5;
constexpr uint8_t PCA_CH_FLY_R_IN1  = 6;
constexpr uint8_t PCA_CH_FLY_R_IN2  = 7;
constexpr uint8_t PCA_CH_XSHUT[3]   = {8, 9, 10};      // left, centre, right
constexpr uint8_t PCA_CH_SERVO_5V   = 11;

// ------------------------------------------------------------- geometry ----
constexpr float WHEEL_DIAMETER_M = 0.072f;
constexpr float TRACK_M          = 0.260f;
constexpr float TICKS_PER_REV    = 1320.0f;  // x4 quadrature, wheel shaft

// --------------------------------------------------------------- limits ----
// The MCU cannot tell "auto" from "teleop": it clamps to the higher teleop
// limit (0.50 m/s). The 0.40 m/s auto limit is enforced by the host (Nav2).
constexpr float V_MAX_DEFAULT  = 0.50f;  // m/s
constexpr float W_MAX_DEFAULT  = 2.0f;   // rad/s
constexpr float A_MAX_DEFAULT  = 0.6f;   // m/s^2
constexpr float ALPHA_MAX      = 3.0f;   // rad/s^2 (no SET_PARAM id in spec)

// --------------------------------------------------------- servo timing ----
constexpr uint16_t SERVO_MIN_US        = 500;
constexpr uint16_t SERVO_MAX_US        = 2500;
constexpr uint16_t SERVO_CENTER_US     = 1500;
constexpr uint16_t GATE_CLOSED_US      = 1000;
constexpr uint16_t GATE_OPEN_US        = 1900;

// ------------------------------------------------------------ scheduler ----
constexpr uint32_t CONTROL_HZ      = 200;
constexpr uint32_t STATE_TX_HZ     = 50;
constexpr uint32_t HOST_WATCHDOG_MS = 250;   // CMD_VEL/PING timeout (spec §2)
constexpr uint32_t HW_WATCHDOG_MS   = 300;   // RP2350 hardware watchdog

// ---------------------------------------------------------------- motors ---
constexpr uint32_t MOTOR_PWM_HZ = 20000;
// Direction polarity: the right motor is mirrored on the chassis.
// TODO(calibration): verify on hardware that +duty drives each wheel forward.
constexpr bool MOTOR_L_INVERT = false;
constexpr bool MOTOR_R_INVERT = true;
constexpr bool ENC_L_INVERT   = false;
constexpr bool ENC_R_INVERT   = true;

// Feed-forward model: duty = v / FF_V_AT_FULL_DUTY + sign(v) * FF_STATIC_DUTY.
// TODO(calibration): measure no-load wheel speed at 100 % duty on 4S nominal.
constexpr float FF_V_AT_FULL_DUTY = 0.80f;  // m/s at duty 1.0
constexpr float FF_STATIC_DUTY    = 0.04f;  // overcomes static friction

// ------------------------------------------------------------------ power --
// TODO(calibration): shunt value of the INA226 board and current sign.
constexpr float INA226_SHUNT_OHM   = 0.005f;
constexpr bool  INA226_INVERT_SIGN = false;  // positive = into battery (charging)
constexpr float VBAT_DIVIDER       = 6.0f;
constexpr float PACK_R_INT_OHM     = 0.12f;  // 4S pack internal resistance, TODO(calibration)
constexpr uint8_t SOC_LOW_PCT      = 20;
constexpr uint8_t SOC_CRITICAL_PCT = 8;
constexpr int16_t CHARGE_DETECT_MA = 50;

// ------------------------------------------------------------------ dock ---
constexpr float DOCK_THRESHOLD_V = 1.5f;
constexpr float DOCK_HYST_V      = 0.1f;

// ----------------------------------------------------------------- feeder --
constexpr float    FEED_MAX_G            = 60.0f;   // spec §4.2 range 1..60 g
constexpr uint32_t FEED_STATIONARY_MS    = 500;     // spec §2 safety rule
constexpr uint32_t FEED_TIMEOUT_MS       = 60000;
constexpr uint32_t FEED_JAM_NO_HALL_MS   = 1500;    // no Hall pulse while driven = jam
constexpr uint32_t FEED_UNJAM_REVERSE_MS = 400;
constexpr uint8_t  FEED_MAX_UNJAM_TRIES  = 2;
constexpr uint8_t  FEED_EMPTY_REVS       = 6;       // revs with no weight change = empty
constexpr float    HOPPER_LOW_G          = 50.0f;
constexpr float    FEED_AUGER_DUTY       = 0.85f;
constexpr float    FEED_AUGER_SLOW_DUTY  = 0.45f;
constexpr float    FEED_SLOW_ZONE_G      = 5.0f;    // slow down in the last grams
constexpr float    FEED_INFLIGHT_G       = 0.8f;    // food still falling after stop, TODO(calibration)

// --------------------------------------------------------------- launcher --
constexpr uint32_t LAUNCH_SPINUP_MS      = 900;   // open-loop flywheel spin-up, TODO(calibration)
constexpr uint32_t LAUNCH_GATE_OPEN_MAX_MS = 700;
constexpr uint32_t LAUNCH_RECOVER_MS     = 500;   // flywheel recovery between balls
constexpr uint32_t LAUNCH_NO_BALL_MS     = 1200;  // wait for a ball before "empty"

// --------------------------------------------------------------- LEDs -----
constexpr unsigned LED_RING_COUNT = 24;
constexpr unsigned LED_EAR_COUNT  = 2;
constexpr unsigned LED_COUNT      = LED_RING_COUNT + LED_EAR_COUNT;

// --------------------------------------------------------------- button ----
constexpr uint32_t BTN_DEBOUNCE_MS  = 25;
constexpr uint32_t BTN_LONG_MS      = 2000;
constexpr uint32_t BTN_PROVISION_MS = 5000;

}  // namespace cfg
