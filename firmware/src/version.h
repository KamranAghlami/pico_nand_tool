/* Firmware version. FW_VERSION_* come from firmware/CMakeLists.txt; FW_GIT_DESC from the build-time generated
 * git_desc.h (firmware/cmake/git_desc.cmake). */
#ifndef VERSION_H
#define VERSION_H

#ifndef FW_VERSION_MAJOR
#error "FW_VERSION_* must be defined by the build"
#endif
#include "git_desc.h"

#define FW_STR_(x) #x
#define FW_STR(x) FW_STR_(x)
#define FW_VERSION_STRING                                                                              \
    "pico-nand-tool " FW_STR(FW_VERSION_MAJOR) "." FW_STR(FW_VERSION_MINOR) "." FW_STR(FW_VERSION_PATCH) \
    " (" FW_GIT_DESC ")"

#endif
