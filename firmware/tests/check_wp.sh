#!/bin/sh
# WP# guard (docs/SPEC.md "Hard safety constraints"): WP# is low except during one armed program/erase. PIN_WP/MASK_WP
# may appear only in pins.h and on these lines of nand_bus.c: the boot init (init, clear latch, enable driver), the
# lowering (write window close, park) and exactly one raise, inside nand_bus_write_window_open(). Anything else, e.g.
# gpio_put(PIN_WP, 1), gpio_pull_up(PIN_WP) or a second raise, fails the build.  Usage: check_wp.sh <firmware src dir>
set -eu
src=$1
allowed='gpio_(init_mask|clr_mask|set_dir_out_masked|set_mask)\(MASK_WP\);( +/\*.*)?'
bad=$(grep -rn --include='*.c' --include='*.h' -E '(PIN|MASK)_WP\b' "$src" \
    | grep -v "^$src/pins\.h:" \
    | grep -Ev "^$src/nand_bus\.c:[0-9]*:    $allowed\$" \
    || true)
if [ -n "$bad" ]; then
    echo "FAIL: WP# used outside its allowed lines in nand_bus.c:"
    echo "$bad"
    exit 1
fi
# The function each raise sits in (the last top-level line that opens a function body).
raises=$(awk '/^[A-Za-z_].*\) *\{$/ { fn = $0 } /gpio_set_mask\(MASK_WP\)/ { print FNR ": " fn }' "$src/nand_bus.c")
n=$(printf '%s' "$raises" | grep -c . || true)
if [ "$n" -ne 1 ] || ! printf '%s' "$raises" | grep -q 'nand_bus_write_window_open)(void) {$'; then
    echo "FAIL: WP# must be raised exactly once, inside nand_bus_write_window_open(); found $n raise(s):"
    echo "$raises"
    exit 1
fi
