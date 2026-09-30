/*
 * Opcode-gate test (docs/SPEC.md "Hard safety constraints"). Compiled on the host by `make -C firmware/tests check`:
 *   -DPOSITIVE_CONTROL : every allowed opcode   -> MUST compile (so the negative cases can't pass by accident)
 *   (default)          : program opcode 80h     -> MUST fail with "forbidden NAND opcode"
 *   -DNON_CONSTANT     : runtime-computed opcode -> MUST fail (the gate only accepts compile-time constants)
 */
#include <stdint.h>

#include "nand_cmd.h"

void nand_bus_cmd_latch_(nand_cmd_t op) { (void)op; }

void gate_test(uint8_t runtime_value) {
    (void)runtime_value;
#if defined(POSITIVE_CONTROL)
    NAND_CMD(NAND_CMD_READ_1);
    NAND_CMD(NAND_CMD_READ_2);
    NAND_CMD(NAND_CMD_READ_STATUS);
    NAND_CMD(NAND_CMD_READ_ID);
    NAND_CMD(NAND_CMD_READ_PARAM);
    NAND_CMD(NAND_CMD_RESET);
#elif defined(NON_CONSTANT)
    NAND_CMD(runtime_value);
#else
    NAND_CMD(0x80); /* Serial Data Input (program setup): must never compile */
#endif
}
