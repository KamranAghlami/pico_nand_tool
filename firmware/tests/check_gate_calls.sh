#!/bin/sh
# Opcode gate, layer 3b (see src/nand_cmd.h): nand_bus_cmd_latch_ may only appear in nand_cmd.h (declaration + the
# NAND_CMD macro) and on its definition line in nand_bus.c. Any other mention could be a direct call that bypasses
# the compile-time allow-list.  Usage: check_gate_calls.sh <firmware src dir>
set -eu
src=$1
bad=$(grep -rn --include='*.c' --include='*.h' 'nand_bus_cmd_latch_' "$src" \
    | grep -v "^$src/nand_cmd\.h:" \
    | grep -v "^$src/nand_bus\.c:[0-9]*:void nand_bus_cmd_latch_(nand_cmd_t op) {\$" || true)
if [ -n "$bad" ]; then
    echo "FAIL: nand_bus_cmd_latch_ used outside the NAND_CMD() gate:"
    echo "$bad"
    exit 1
fi
