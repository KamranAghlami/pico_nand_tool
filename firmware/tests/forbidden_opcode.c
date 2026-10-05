/*
 * Opcode-gate test (docs/SPEC.md "Hard safety constraints"). Compiled on the host by `make -C firmware/tests check`:
 *   -DPOSITIVE_CONTROL : every allowed opcode through its own gate -> MUST compile (so the negative cases can't pass
 *                        by accident)
 *   (default)          : program opcode 80h through the read gate  -> MUST fail with "forbidden NAND opcode"
 *   -DNON_CONSTANT     : runtime-computed opcode, read gate         -> MUST fail (only compile-time constants)
 *   -DWRITE_85         : 85h (random data input) through the write gate -> MUST fail with "forbidden NAND write opcode"
 *   -DWRITE_READ_OP    : a read opcode (70h) through the write gate -> MUST fail with "forbidden NAND write opcode"
 *   -DWRITE_NON_CONSTANT : runtime-computed opcode, write gate     -> MUST fail
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
    NAND_WCMD(NAND_CMD_PROGRAM_1);
    NAND_WCMD(NAND_CMD_PROGRAM_2);
    NAND_WCMD(NAND_CMD_ERASE_1);
    NAND_WCMD(NAND_CMD_ERASE_2);
#elif defined(NON_CONSTANT)
    NAND_CMD(runtime_value);
#elif defined(WRITE_85)
    NAND_WCMD(0x85); /* Random Data Input: never allowed */
#elif defined(WRITE_READ_OP)
    NAND_WCMD(0x70);
#elif defined(WRITE_NON_CONSTANT)
    NAND_WCMD(runtime_value);
#else
    NAND_CMD(0x80); /* Serial Data Input (program setup): never through the read gate */
#endif
}
