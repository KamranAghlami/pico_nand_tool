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

execute_process(
    COMMAND git describe --always --dirty --abbrev=7
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    OUTPUT_VARIABLE FW_GIT_DESC
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET)
if(NOT FW_GIT_DESC)
    set(FW_GIT_DESC "unknown")
endif()

add_executable(pico_nand_tool
    src/main.c
    src/protocol.c
    src/frame.c
    src/crc32.c
    src/timing.c
    src/nand_bus.c
    src/usb_descriptors.c
)

target_include_directories(pico_nand_tool PRIVATE src)

target_compile_definitions(pico_nand_tool PRIVATE
    FW_VERSION_MAJOR=${FW_VERSION_MAJOR}
    FW_VERSION_MINOR=${FW_VERSION_MINOR}
    FW_VERSION_PATCH=${FW_VERSION_PATCH}
    FW_GIT_DESC="${FW_GIT_DESC}"
)

target_compile_options(pico_nand_tool PRIVATE -Wall -Wextra)

target_link_libraries(pico_nand_tool
    pico_stdlib
    pico_unique_id
    pico_bootrom
    hardware_gpio
    hardware_clocks
    tinyusb_device
)

# The data channel is our own raw CDC interface, so no SDK stdio at all. stdio_uart would also claim GP0/GP1, which
# are NAND IO0/IO1.
pico_enable_stdio_usb(pico_nand_tool 0)
pico_enable_stdio_uart(pico_nand_tool 0)

pico_add_extra_outputs(pico_nand_tool)
