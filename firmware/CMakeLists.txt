cmake_minimum_required(VERSION 3.13)

# Pico SDK build: pico_sdk_import.cmake finds the SDK (PICO_SDK_PATH), pico_sdk_init() sets up the cross toolchain
# (arm-none-eabi-gcc for the Cortex-M0+), and pico_add_extra_outputs() turns the ELF into pico_nand_tool.uf2, the
# file you drag onto the RPI-RP2 drive. Only the pure modules (crc32, frame, timing) are also built for the PC, by
# firmware/tests/Makefile.

# Target board: Raspberry Pi Pico (RP2040).
set(PICO_BOARD pico CACHE STRING "Board type")

include(pico_sdk_import.cmake)

project(pico_nand_tool C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

pico_sdk_init()

# Firmware version, reported by PING. Release builds take it from the git tag (CI: tag v1.2.3 -> -DFW_VERSION=1.2.3);
# local builds use this default. No leading zeros: each part becomes a C integer literal (08 would be octal).
set(FW_VERSION "0.2.0" CACHE STRING "Firmware version X.Y.Z")
if(NOT FW_VERSION MATCHES "^(0|[1-9][0-9]?[0-9]?)\\.(0|[1-9][0-9]?[0-9]?)\\.(0|[1-9][0-9]?[0-9]?)$")
    message(FATAL_ERROR "FW_VERSION '${FW_VERSION}' is not X.Y.Z (each 0..999, no leading zeros)")
endif()
set(FW_VERSION_MAJOR ${CMAKE_MATCH_1})
set(FW_VERSION_MINOR ${CMAKE_MATCH_2})
set(FW_VERSION_PATCH ${CMAKE_MATCH_3})
if(FW_VERSION_MAJOR GREATER 255 OR FW_VERSION_MINOR GREATER 255 OR FW_VERSION_PATCH GREATER 255)
    message(FATAL_ERROR "FW_VERSION '${FW_VERSION}': each part must fit the u8 PING fields (0..255)")
endif()
message(STATUS "Firmware version ${FW_VERSION}")

# git description for the PING version string, regenerated on every build (not just at configure time).
set(GIT_DESC_H ${CMAKE_CURRENT_BINARY_DIR}/generated/git_desc.h)
add_custom_target(git_desc ALL
    COMMAND ${CMAKE_COMMAND} -DSRC_DIR=${CMAKE_CURRENT_SOURCE_DIR} -DOUT=${GIT_DESC_H}
            -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/git_desc.cmake
    BYPRODUCTS ${GIT_DESC_H}
    COMMENT "Updating git description"
    VERBATIM)

add_executable(pico_nand_tool
    src/main.c
    src/protocol.c
    src/frame.c
    src/crc32.c
    src/timing.c
    src/nand_bus.c
    src/nand_ops.c
    src/nand_write.c
    src/fault.c
    src/usb_descriptors.c
)

target_include_directories(pico_nand_tool PRIVATE src ${CMAKE_CURRENT_BINARY_DIR}/generated)
add_dependencies(pico_nand_tool git_desc)

target_compile_definitions(pico_nand_tool PRIVATE
    FW_VERSION_MAJOR=${FW_VERSION_MAJOR}
    FW_VERSION_MINOR=${FW_VERSION_MINOR}
    FW_VERSION_PATCH=${FW_VERSION_PATCH}
    # Every panic() -> fault.c: park the NAND bus (WP# low) and halt.
    PICO_PANIC_FUNCTION=nand_tool_panic
)

target_compile_options(pico_nand_tool PRIVATE -Wall -Wextra)

target_link_libraries(pico_nand_tool
    pico_stdlib
    pico_unique_id
    pico_bootrom
    hardware_gpio
    hardware_clocks
    hardware_sync
    hardware_timer
    hardware_watchdog
    tinyusb_device
)

# The data channel is our own raw CDC interface, so no SDK stdio at all. stdio_uart would also claim GP0/GP1, which
# are NAND IO0/IO1.
pico_enable_stdio_usb(pico_nand_tool 0)
pico_enable_stdio_uart(pico_nand_tool 0)

pico_add_extra_outputs(pico_nand_tool)
