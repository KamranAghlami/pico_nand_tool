/* Firmware version. FW_VERSION_* and FW_GIT_DESC come from CMake (firmware/CMakeLists.txt). */
#ifndef VERSION_H
#define VERSION_H

#ifndef FW_VERSION_MAJOR
#error "FW_VERSION_* must be defined by the build"
#endif
#ifndef FW_GIT_DESC
#define FW_GIT_DESC "unknown"
#endif

#define FW_STR_(x) #x
#define FW_STR(x) FW_STR_(x)
#define FW_VERSION_STRING                                                                              \
    "pico-nand-tool " FW_STR(FW_VERSION_MAJOR) "." FW_STR(FW_VERSION_MINOR) "." FW_STR(FW_VERSION_PATCH) \
    " (" FW_GIT_DESC ")"

#endif
