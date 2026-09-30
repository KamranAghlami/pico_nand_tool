/*
 * THE ONLY NAND OPCODES THIS FIRMWARE CAN EVER PUT ON THE BUS (docs/SPEC.md "Hard safety constraints").
 * Program/erase opcodes (80h 10h 85h 60h D0h ...) are deliberately absent and must never be added.
 *
 * Gate, in layers:
 *   1. NAND_CMD(op): _Static_assert that op is a compile-time constant from the allow-list below.
 *   2. nand_bus_cmd_latch_() re-checks with nand_cmd_is_allowed() and panics BEFORE touching the bus.
 *   3. `make -C firmware/tests check` (run in CI) proves NAND_CMD(0x80) and non-constant opcodes don't compile, and
 *      fails if any source other than this header's macro and the definition in nand_bus.c names
 *      nand_bus_cmd_latch_ (so the static layer can't be bypassed by calling it directly).
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

/* Runtime form of the allow-list (layer 2). */
static inline int nand_cmd_is_allowed(unsigned op) { return NAND_CMD_IS_ALLOWED(op); }

/* Command latch cycle (Fig. 12). NEVER call directly (enforced by `make check`); use NAND_CMD(). */
void nand_bus_cmd_latch_(nand_cmd_t op);

#define NAND_CMD(op)                                                                 \
    do {                                                                             \
        _Static_assert(NAND_CMD_IS_ALLOWED(op), "forbidden NAND opcode");            \
        nand_bus_cmd_latch_((nand_cmd_t)(op));                                       \
    } while (0)

#endif
