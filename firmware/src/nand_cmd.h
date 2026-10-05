/*
 * THE ONLY NAND OPCODES THIS FIRMWARE CAN EVER PUT ON THE BUS (docs/SPEC.md "Hard safety constraints").
 *   Read side,  NAND_CMD():  00h 30h 70h 90h ECh FFh
 *   Write side, NAND_WCMD(): 80h 10h (Page Program, full page)  60h D0h (Block Erase)
 * Everything else (85h random data input, copy-back, cache, multiplane, 78h, OTP ...) is deliberately absent and must
 * never be added without a SPEC change.
 *
 * Gate, in layers:
 *   1. NAND_CMD(op) / NAND_WCMD(op): _Static_assert that op is a compile-time constant from that macro's list.
 *   2. nand_bus_cmd_latch_() re-checks with nand_cmd_is_allowed() and panics BEFORE touching the bus. A write opcode
 *      also panics outside the write window (WP# high, nand_bus.c) or out of sequence: 10h only after 80h + 5 address
 *      cycles + data input, D0h only after 60h + 3 address cycles.
 *   3. `make -C firmware/tests check` (run in CI) proves forbidden and non-constant opcodes don't compile, and
 *      check_gate_calls.sh fails if nand_bus_cmd_latch_ is named outside this header and its definition, or
 *      NAND_WCMD / the data-input cycle / the write window are used outside nand_write.c.
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
    NAND_CMD_PROGRAM_1 = 0x80,   /* §3.2 Page Program: Serial Data Input (write mode only)             */
    NAND_CMD_PROGRAM_2 = 0x10,   /* §3.2 Page Program: confirm (write mode only)                       */
    NAND_CMD_ERASE_1 = 0x60,     /* §3.5 Block Erase: setup (write mode only)                          */
    NAND_CMD_ERASE_2 = 0xD0,     /* §3.5 Block Erase: confirm (write mode only)                        */
} nand_cmd_t;

#define NAND_CMD_IS_READ(op) \
    ((op) == 0x00 || (op) == 0x30 || (op) == 0x70 || (op) == 0x90 || (op) == 0xEC || (op) == 0xFF)
#define NAND_CMD_IS_WRITE(op) ((op) == 0x80 || (op) == 0x10 || (op) == 0x60 || (op) == 0xD0)

/* Runtime form of the allow-lists (layer 2). */
static inline int nand_cmd_is_read(unsigned op) { return NAND_CMD_IS_READ(op); }
static inline int nand_cmd_is_write(unsigned op) { return NAND_CMD_IS_WRITE(op); }
static inline int nand_cmd_is_allowed(unsigned op) { return NAND_CMD_IS_READ(op) || NAND_CMD_IS_WRITE(op); }

/* Command latch cycle (Fig. 12). NEVER call directly (enforced by `make check`); use NAND_CMD() / NAND_WCMD(). */
void nand_bus_cmd_latch_(nand_cmd_t op);

#define NAND_CMD(op)                                                                 \
    do {                                                                             \
        _Static_assert(NAND_CMD_IS_READ(op), "forbidden NAND opcode");               \
        nand_bus_cmd_latch_((nand_cmd_t)(op));                                       \
    } while (0)

/* Write opcodes: only in nand_write.c (check_gate_calls.sh), only inside the write window (runtime). */
#define NAND_WCMD(op)                                                                \
    do {                                                                             \
        _Static_assert(NAND_CMD_IS_WRITE(op), "forbidden NAND write opcode");        \
        nand_bus_cmd_latch_((nand_cmd_t)(op));                                       \
    } while (0)

#endif
