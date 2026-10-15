# Petmate MCU firmware - Raspberry Pi Pico 2 (RP2350), Pico SDK >= 2.0
#
#   export PICO_SDK_PATH=/path/to/pico-sdk
#   cmake -S . -B build -DPICO_BOARD=pico2 && cmake --build build -j
cmake_minimum_required(VERSION 3.13)

if(NOT DEFINED PICO_BOARD)
    set(PICO_BOARD pico2 CACHE STRING "Board type")
endif()

include(pico_sdk_import.cmake)

project(petmate_fw C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

pico_sdk_init()

add_executable(petmate_fw
    src/main.cpp
    src/protocol.cpp
    src/control.cpp
    src/usb_link.cpp
    src/usb/usb_descriptors.c
    src/params.cpp
    src/events.cpp
    src/drive.cpp
    src/safety.cpp
    src/power.cpp
    src/cliff.cpp
    src/tof.cpp
    src/inputs.cpp
    src/feeder.cpp
    src/launcher.cpp
    src/teaser.cpp
    src/leds.cpp
    src/drivers/i2c_bus.cpp
    src/drivers/pca9685.cpp
    src/drivers/servo.cpp
    src/drivers/motors.cpp
    src/drivers/encoders.cpp
    src/drivers/icm42688.cpp
    src/drivers/hx711.cpp
    src/drivers/vl53l1x.cpp
)

pico_generate_pio_header(petmate_fw ${CMAKE_CURRENT_LIST_DIR}/src/pio/quadrature_encoder.pio)
pico_generate_pio_header(petmate_fw ${CMAKE_CURRENT_LIST_DIR}/src/pio/ws2812.pio)

target_include_directories(petmate_fw PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/src
    ${CMAKE_CURRENT_LIST_DIR}/src/usb   # tusb_config.h
)

target_compile_options(petmate_fw PRIVATE -Wall -Wextra -Wno-unused-parameter
    $<$<COMPILE_LANGUAGE:CXX>:-fno-exceptions -fno-rtti>)

target_link_libraries(petmate_fw
    pico_stdlib
    pico_unique_id
    pico_rand
    pico_flash
    tinyusb_device
    hardware_pwm
    hardware_pio
    hardware_i2c
    hardware_adc
    hardware_watchdog
    hardware_flash
)

# Binary protocol owns the USB CDC port: no stdio on USB or UART.
pico_enable_stdio_usb(petmate_fw 0)
pico_enable_stdio_uart(petmate_fw 0)

pico_set_program_name(petmate_fw "petmate_fw")
pico_set_program_version(petmate_fw "0.1.0")
pico_add_extra_outputs(petmate_fw)   # .uf2, .hex, .bin, .dis
