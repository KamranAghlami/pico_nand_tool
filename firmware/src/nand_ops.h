/* Datasheet command sequences on top of nand_bus. Each returns a PROTO_ST_* status (protocol_defs.h). */
#ifndef NAND_OPS_H
#define NAND_OPS_H

#include <stdint.h>

/* Power-on wait (§4.1: busy ≤ 5 ms after power-up), then FFh and wait ready (§3.12, Fig. 33).
 * *busy_cycles = clk_sys cycles from just after the FFh latch (WE# high + t_wh) until R/B# was seen high again,
 * 0 if R/B# was never seen low. */
uint8_t nand_reset(uint32_t *busy_cycles);

/* 90h + addr, then n bytes (§3.16, Fig. 41 for addr 00h; §3.18, Fig. 43 for addr 20h "ONFI"). */
uint8_t nand_read_id(uint8_t addr, uint8_t *buf, uint32_t n);

/* Dummy 00h (§3.16 note), then 70h and one status byte (§3.9, Fig. 31; bits: §3.11, Table 13). */
uint8_t nand_read_status(uint8_t *sr);

/* FFh first (§3.19 note: required for 41 nm 2 Gb parts), then ECh + 00h, wait tR on R/B#, read n bytes (§3.19,
 * Fig. 44). n = 768 gives the three redundant 256-byte copies (Table 3.4). */
uint8_t nand_read_param(uint8_t *buf, uint32_t n);

/* 00h, 5 address cycles (column 0, row = page, Table 5), 30h, wait tR on R/B#, read n bytes from column 0 (§3.1,
 * Fig. 6.1). n = 2112 is the whole page: 2048 data + 64 spare. */
uint8_t nand_read_page(uint32_t page, uint8_t *buf, uint32_t n);

#endif
