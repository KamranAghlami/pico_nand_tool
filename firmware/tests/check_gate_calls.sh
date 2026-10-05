#!/bin/sh
# Opcode gate, layer 3b (see src/nand_cmd.h).
# 1. nand_bus_cmd_latch_ may only appear in nand_cmd.h (declaration + the NAND_CMD/NAND_WCMD macros) and on its
#    definition line in nand_bus.c. Any other mention could be a direct call that bypasses the compile-time lists.
# 2. The write path (NAND_WCMD, nand_bus_data_in, nand_bus_write_window_open/close) may only be used in nand_write.c,
#    besides nand_cmd.h, the declarations in nand_bus.h and the definition lines in nand_bus.c.
# Usage: check_gate_calls.sh <firmware src dir>
set -eu
src=$1
bad=$(grep -rn --include='*.c' --include='*.h' 'nand_bus_cmd_latch_' "$src" \
    | grep -v "^$src/nand_cmd\.h:" \
    | grep -v "^$src/nand_bus\.c:[0-9]*:void nand_bus_cmd_latch_(nand_cmd_t op) {\$" || true)
if [ -n "$bad" ]; then
    echo "FAIL: nand_bus_cmd_latch_ used outside the NAND_CMD()/NAND_WCMD() gate:"
    echo "$bad"
    exit 1
fi
write_path='NAND_WCMD|nand_bus_data_in|nand_bus_write_window_(open|close)'
bad=$(grep -rn --include='*.c' --include='*.h' -E "\\b($write_path)\\b" "$src" \
    | grep -v "^$src/nand_write\.c:" \
    | grep -v "^$src/nand_cmd\.h:" \
    | grep -v "^$src/nand_bus\.h:" \
    | grep -Ev "^$src/nand_bus\.c:[0-9]*:void __not_in_flash_func\(($write_path)\)\(" \
    || true)
if [ -n "$bad" ]; then
    echo "FAIL: write path used outside nand_write.c:"
    echo "$bad"
    exit 1
fi
