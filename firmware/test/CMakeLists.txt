# Host-side unit tests (no Pico SDK). Usage:
#   cmake -S firmware/test -B build-test && cmake --build build-test && ctest --test-dir build-test
cmake_minimum_required(VERSION 3.13)
project(petmate_host_tests CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(FW_SRC ${CMAKE_CURRENT_LIST_DIR}/../src)
add_executable(petmate_tests
    test_protocol.cpp
    ${FW_SRC}/protocol.cpp
    ${FW_SRC}/control.cpp)
target_include_directories(petmate_tests PRIVATE ${FW_SRC})
target_compile_options(petmate_tests PRIVATE -Wall -Wextra -Werror -O1 -fsanitize=address,undefined)
target_link_options(petmate_tests PRIVATE -fsanitize=address,undefined)

enable_testing()
add_test(NAME protocol COMMAND petmate_tests)
