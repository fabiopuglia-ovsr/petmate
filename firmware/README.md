# Petmate MCU firmware (RP2350 / Raspberry Pi Pico 2)

Firmware for the Petmate robot's microcontroller: differential drive, sensors,
food dispenser, ball launcher, teaser arm, LEDs and safety supervisor. It talks
to the host (ROS 2 `petmate_base`) over USB CDC using the COBS + CRC16 protocol.

**Source of truth:** `../docs/INTERFACES.md` §1 (pinout, I2C map, geometry,
limits) and §2 (serial protocol, safety rules). `src/config.h` mirrors §1.

## Build

Requirements: Pico SDK ≥ 2.0 (tested with 2.1.1, TinyUSB submodule required),
`arm-none-eabi-gcc` (tested 13.2.1), CMake ≥ 3.13, Python 3, Ninja or Make.

```bash
git clone --branch 2.1.1 https://github.com/raspberrypi/pico-sdk.git
git -C pico-sdk submodule update --init lib/tinyusb
export PICO_SDK_PATH=$PWD/pico-sdk

cd firmware
cmake -S . -B build -G Ninja -DPICO_BOARD=pico2
cmake --build build
# -> build/petmate_fw.uf2 (also .elf, .bin, .hex, .dis)
```

`PICO_BOARD` defaults to `pico2` (RP2350, ARM Cortex-M33). `pico_sdk_import.cmake`
is the stock copy from the SDK and locates the SDK through `PICO_SDK_PATH`.

## Flashing

- **BOOTSEL:** hold BOOTSEL while plugging in USB, then copy
  `build/petmate_fw.uf2` to the `RP2350` drive.
- **picotool:** `picotool load -f build/petmate_fw.uf2 && picotool reboot`
- **Without the button:** opening the CDC port at **1200 baud** reboots the
  running firmware into BOOTSEL (`stty -F /dev/petmate_mcu 1200`). Normal
  clients must never pick 1200 baud.
- **SWD:** `openocd -f interface/cmsis-dap.cfg -f target/rp2350.cfg -c "program build/petmate_fw.elf verify reset exit"`

## udev rule

```bash
sudo cp udev/99-petmate.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
ls -l /dev/petmate_mcu
```

The device enumerates as VID `0x2E8A`, PID `0x0009`, product `"Petmate MCU"`,
serial = the RP2350 unique ID. The rule matches VID and product string. It also
tells ModemManager to ignore the port.

## Host tools and tests

```bash
# Unit tests (plain gcc, no SDK): COBS/CRC vectors, fuzzing, packet sizes,
# LE layout, golden frames shared with the Python tool, control math.
cmake -S test -B build-test && cmake --build build-test && ctest --test-dir build-test

# Python console (pip install pyserial)
python3 tools/mcu_console.py --selftest             # offline, no hardware
python3 tools/mcu_console.py /dev/petmate_mcu ping
python3 tools/mcu_console.py /dev/petmate_mcu state --count 10
python3 tools/mcu_console.py /dev/petmate_mcu vel 0.15 0 --duration 2
python3 tools/mcu_console.py /dev/petmate_mcu feed 25
python3 tools/mcu_console.py /dev/petmate_mcu param kp 1.2
python3 tools/mcu_console.py /dev/petmate_mcu monitor
```

## Architecture

Single core, cooperative super-loop (`src/main.cpp`). Every task is
non-blocking and runs on its own period from the 1 MHz system timer:

| Task | Rate | Content |
|---|---|---|
| USB poll | every pass | TinyUSB service, COBS stream decoding, command dispatch |
| Cliff ADC | ~3 ms/channel | ADS1115 single-shot mux (4 ch, ~80 Hz per sensor) |
| Control | 200 Hz | inputs debounce, safety, IMU read, wheel PID, odometry |
| STATE TX | 50 Hz | 52-byte STATE frame |
| Actuators | 50 Hz | feeder (HX711 10 SPS), launcher + intake, teaser, servo rail |
| LEDs | 50 Hz | WS2812 via PIO |
| ToF | 20 Hz | 3× VL53L1X poll |
| Power | 10 Hz | INA226 / ADC, SoC, battery events |
| Params | 1 Hz | flash save when stationary and idle |

Hardware watchdog: 300 ms, fed only while the control task has run within the
last 50 ms. A watchdog reboot is reported in the boot LOG message.

| Module | Files | Notes |
|---|---|---|
| Protocol | `protocol.*` | COBS, CRC-16/CCITT-FALSE, payload structs with explicit LE (de)serialisation, stream decoder. No SDK dependency. |
| USB link | `usb_link.*`, `usb/` | TinyUSB CDC used directly (see below), non-blocking TX, LOG helper |
| Motors | `drivers/motors.*` | DRV8874 PH/EN, 20 kHz PWM, nFAULT input |
| Encoders | `drivers/encoders.*`, `pio/quadrature_encoder.pio` | pico-examples PIO decoder (x4) |
| Drive | `drive.*`, `control.*` | PID with feed-forward + anti-windup, accel ramps (a_max, α_max), limits, RK2 odometry |
| IMU | `drivers/icm42688.*` | ±4 g, ±500 dps, 1 kHz ODR, gyro bias learned while stationary |
| Power | `power.*` | INA226 primary, ADC0 backup, IR-compensated 4S SoC curve |
| Cliff | `cliff.*` | ADS1115 + Sharp GP2Y0A51 V→mm table, debounce, fail-safe |
| ToF | `tof.*`, `drivers/vl53l1x.*` | XSHUT via PCA9685, re-addressing to 0x30/31/32 |
| PCA9685 | `drivers/pca9685.*` | 1.5 kHz, cached writes, DRV8833 helper |
| Feeder | `feeder.*`, `drivers/hx711.*` | weight closed loop, Hall jam detection, unjam reverse, timeout, tare |
| Launcher | `launcher.*` | flywheel spin-up, gate servo, IR ball count, intake roller |
| Teaser | `teaser.*` | wiggle / sweep / random patterns on GP0 |
| LEDs | `leds.*`, `pio/ws2812.pio` | off / solid / breathe / rainbow / chase + safety overlays |
| Inputs | `inputs.*` | bumpers, wheel drop, paw button (short/long/5 s), dock ADC |
| Safety | `safety.*` | host watchdog, directional blocking, ESTOP/nFAULT |
| Events | `events.*` | queued EVENT frames, survive short host stalls |
| Params | `params.*` | SET_PARAM ids 1–11, range checks, flash persistence |

### Design decisions

- **TinyUSB directly, not `pico_stdio_usb`.** The stdio write path can block
  for up to 500 ms when the host stops reading, which would stall the 200 Hz
  loop and trip the watchdog. `usb_link` writes whole frames only when the TX
  FIFO has room and otherwise drops and counts them.
- **Safety stops are not ramped.** The host watchdog uses the deceleration ramp
  (spec §2). A blocked direction (cliff/bumper) zeroes the setpoint at once.
  Wheel drop, ESTOP and nFAULT remove drive at once.
- **Wheel drop** stops all motion and also stops the auger, flywheels, intake
  and teaser. A lifted robot should not move anything.
- **ESTOP** additionally cuts the servo 5 V rail and stops the intake and
  teaser. It is latched until `ESTOP 0`.
- **Fail-safe sensors:** if the ADS1115 stops answering, all four cliff bits
  are reported set (blocks v≠0). Bumper and wheel-drop inputs start asserted
  until the first debounce window has passed.
- **FEED is answered:** a rejected FEED (busy, out of 0.1–60 g, safety inhibit)
  produces `FEED_STATUS state=aborted` plus a LOG with the reason.
- **Max speed:** the MCU clamps to the teleop limit 0.50 m/s, because it cannot
  tell auto from teleop. The 0.40 m/s auto limit is applied by the host.
  `SET_PARAM 7/8/9` may only lower v_max, ω_max and a_max below the §1 limits.

### Items needing physical calibration (`TODO(calibration)` in code)

Motor and encoder polarity, feed-forward (`FF_V_AT_FULL_DUTY`, static duty),
PID gains, INA226 shunt and current sign, pack internal resistance and OCV
curve, ADC divider, Sharp V→mm curve on the real floor, IMU mounting axes,
HX711 counts per gram, weight-stability threshold, food in-flight allowance,
flywheel spin-up time and right-flywheel sign, intake roller duty, teaser arm
end stops.

## Spec notes (INTERFACES.md issues, not changed here)

1. **DRV8874 "coast":** §2 requires the wheel motors to coast on ESTOP/nFAULT.
   In PH/EN mode, EN=0 is a low-side *brake*, not coast. Coast needs nSLEEP or
   PMODE control, and the §1 pinout gives no pin for either. The firmware sets
   EN=0. On nFAULT the driver goes Hi-Z by itself, so that case does coast.
   A future board should route nSLEEP to a GPIO (for example via the PCA9685).
2. **ToF data has no message:** §2 defines no field carrying the VL53L1X
   ranges, and no safety rule uses them. The firmware reads them but cannot
   report them.
3. **v_max auto vs teleop:** see "Max speed" above.
4. **α_max (3.0 rad/s²)** has no SET_PARAM id. It is fixed at compile time.
5. Some event `data` meanings are not specified. The firmware uses:
   CLIFF → cliff_mask, BUMP → bump bit, BUTTON_SHORT/LONG → hold time in ms,
   others → 0 unless §2 defines them.
6. A VID-only udev match (`0x2E8A`) would also catch other Raspberry Pi USB
   devices. The rule therefore also matches the product string.
