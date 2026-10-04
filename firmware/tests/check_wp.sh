#!/bin/sh
# WP# guard (docs/SPEC.md "Hard safety constraints"): the firmware may only ever drive WP# LOW. PIN_WP/MASK_WP may
# appear only in pins.h and on the three init lines in nand_bus.c (init, clear latch, enable driver). Anything else,
# e.g. gpio_set_mask(MASK_WP) or gpio_put(PIN_WP, 1), fails the build.  Usage: check_wp.sh <firmware src dir>
set -eu
src=$1
bad=$(grep -rn --include='*.c' --include='*.h' -E '(PIN|MASK)_WP\b' "$src" \
    | grep -v "^$src/pins\.h:" \
    | grep -Ev "^$src/nand_bus\.c:[0-9]*:    gpio_(init_mask|clr_mask|set_dir_out_masked)\(MASK_WP\);\$" || true)
if [ -n "$bad" ]; then
    echo "FAIL: WP# used outside its drive-low init in nand_bus.c:"
    echo "$bad"
    exit 1
fi
