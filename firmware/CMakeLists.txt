cmake_minimum_required(VERSION 3.13)

# Target board: Raspberry Pi Pico (RP2040).
set(PICO_BOARD pico CACHE STRING "Board type")

include(pico_sdk_import.cmake)

project(pico_nand_tool C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

pico_sdk_init()

set(FW_VERSION_MAJOR 0)
set(FW_VERSION_MINOR 1)
set(FW_VERSION_PATCH 0)

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
    src/fault.c
    src/usb_descriptors.c
)

target_include_directories(pico_nand_tool PRIVATE src ${CMAKE_CURRENT_BINARY_DIR}/generated)
add_dependencies(pico_nand_tool git_desc)

target_compile_definitions(pico_nand_tool PRIVATE
    FW_VERSION_MAJOR=${FW_VERSION_MAJOR}
    FW_VERSION_MINOR=${FW_VERSION_MINOR}
    FW_VERSION_PATCH=${FW_VERSION_PATCH}
    # Every panic() -> fault.c: park the NAND bus and halt.
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
    tinyusb_device
)

# The data channel is our own raw CDC interface, so no SDK stdio at all. stdio_uart would also claim GP0/GP1, which
# are NAND IO0/IO1.
pico_enable_stdio_usb(pico_nand_tool 0)
pico_enable_stdio_uart(pico_nand_tool 0)

pico_add_extra_outputs(pico_nand_tool)
