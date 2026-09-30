/*
 * THE ONLY NAND OPCODES THIS FIRMWARE CAN EVER PUT ON THE BUS (docs/SPEC.md "Hard safety constraints").
 * Program/erase opcodes (80h 10h 85h 60h D0h ...) are deliberately absent and must never be added.
 *
 * Gate, in three layers:
 *   1. NAND_CMD(op): _Static_assert that op is a compile-time constant from the allow-list below.
 *   2. nand_bus_cmd_latch_() re-checks at runtime and panics BEFORE touching the bus.
 *   3. firmware/tests/forbidden_opcode.c proves in CI that NAND_CMD(0x80) and non-constant opcodes don't compile.
 * No SDK includes, so the gate test can compile this header on the host.
 */
#ifndef NAND_CMD_H
#define NAND_CMD_H

typedef enum {
    NAND_CMD_READ_1 = 0x00,      /* §3.1 Page Read, 1st cycle (also the dummy before 70h, §3.16 note) */
    NAND_CMD_READ_2 = 0x30,      /* §3.1 Page Read, 2nd cycle                                          */
    NAND_CMD_READ_STATUS = 0x70, /* §3.9 Read Status Register                                          */
    NAND_CMD_READ_ID = 0x90,     /* §3.16 Read ID / §3.18 Read ONFI Signature                          */
    NAND_CMD_READ_PARAM = 0xEC,  /* §3.19 Read Parameter Page                                          */
    NAND_CMD_RESET = 0xFF,       /* §3.12 Reset                                                        */
} nand_cmd_t;

#define NAND_CMD_IS_ALLOWED(op) \
    ((op) == 0x00 || (op) == 0x30 || (op) == 0x70 || (op) == 0x90 || (op) == 0xEC || (op) == 0xFF)

/* Command latch cycle (Fig. 12). Do not call directly; use NAND_CMD(). */
void nand_bus_cmd_latch_(nand_cmd_t op);

#define NAND_CMD(op)                                                                 \
    do {                                                                             \
        _Static_assert(NAND_CMD_IS_ALLOWED(op), "forbidden NAND opcode");            \
        nand_bus_cmd_latch_((nand_cmd_t)(op));                                       \
    } while (0)

#endif
