/* Write mode (docs/PROTOCOL.md "Write mode"): the arm state, Block Erase and Page Program. Each returns a PROTO_ST_*
 * status (protocol_defs.h). The only file that may open the write window or issue write opcodes. */
#ifndef NAND_WRITE_H
#define NAND_WRITE_H

#include <stdbool.h>
#include <stdint.h>

/* Arm for blocks [first_block, last_block] until idle_timeout_s pass without a successful erase/program. ERR_BAD_ARGS
 * (and the previous state kept) on a wrong token or range. A valid call replaces any earlier arm. */
uint8_t nand_write_arm(uint32_t token, uint16_t first_block, uint16_t last_block, uint16_t idle_timeout_s);

void nand_write_disarm(void);

/* Unless ignore_bad_marker: spare byte 0 of pages 0, 1, 63 must be FFh (§9.2), else ERR_BAD_BLOCK. Then 60h, row
 * address, D0h (§3.5, Fig. 24). *sr and *busy_cycles are set for OK, ERR_OP_FAILED and ERR_WP_STUCK. */
uint8_t nand_erase_block(uint16_t block, bool ignore_bad_marker, uint8_t *sr, uint32_t *busy_cycles);

/* 80h, 5 address cycles (column 0), 2112 data bytes, 10h (§3.2, Fig. 19). Same results as nand_erase_block. */
uint8_t nand_program_page(uint32_t page, const uint8_t *data, uint8_t *sr, uint32_t *busy_cycles);

#endif
