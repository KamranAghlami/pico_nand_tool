/* NAND bus primitives over RP2040 SIO (mask operations only). Datasheet sequences built on them: nand_ops.c.
 *
 * Layering: this is the lowest layer, the only code that touches the NAND GPIOs. Each function is one kind of bus
 * cycle (select, address latch, read n bytes, wait for R/B#...), with the timing_t delays built in. It knows
 * nothing about pages or blocks; nand_ops.c / nand_write.c string these cycles together into datasheet commands,
 * and protocol.c turns those into USB requests. Command latch cycles are not declared here: they go through the
 * NAND_CMD() / NAND_WCMD() gate in nand_cmd.h. */
#ifndef NAND_BUS_H
#define NAND_BUS_H

#include <stdbool.h>
#include <stdint.h>

#include "timing.h"

/* Put every NAND line in its safe idle state: CE#/WE#/RE# driven high, CLE/ALE driven low, IO and R/B# inputs.
 * Call first thing in main(). */
void nand_bus_init(void);

/* The timing every phase below uses. The struct may change in place (SET_TIMING); the pointer must stay valid. */
void nand_bus_use_timing(const timing_t *t);

/* Back to the idle state from anywhere (panic handler): WP# low and write window closed, CE# high, then CLE/ALE low,
 * strobes high, IO input. */
void nand_bus_park(void);

/* CE# low, then t_cs. */
void nand_bus_select(void);

/* IO input, CE# high, then t_ceh (tCHZ, tCSD). */
void nand_bus_deselect(void);

/* One address latch cycle (Fig. 13). Commands go through NAND_CMD() in nand_cmd.h. */
void nand_bus_addr(uint8_t a);

/* t_whr after the last WE# rising edge, before the first RE# fall (tWHR, tAR, tCLR). */
void nand_bus_wait_whr(void);

/* After a command: t_wb, then poll R/B# until high for at most rb_timeout_us, then t_rr. False on timeout.
 * *busy_cycles (may be NULL) = clk_sys cycles from entry until R/B# was seen high, or 0 if it was never seen low. */
bool nand_bus_wait_ready(uint32_t *busy_cycles);

/* Same with an explicit R/B# timeout (program 2 ms, erase 20 ms). */
bool nand_bus_wait_ready_us(uint32_t timeout_us, uint32_t *busy_cycles);

/* Power-on wait (§4.1, Fig. 46): poll R/B# until high for at most timeout_us. No command precedes it, so no tWB. */
bool nand_bus_wait_rb_high(uint32_t timeout_us);

/* n data-output cycles (Fig. 15): IO to input, then per byte RE# low, t_rea, sample, RE# high, t_reh. Ends with
 * t_rhw, so the bus may be driven again right after. The caller has already waited t_whr or t_rr. */
void nand_bus_read(uint8_t *buf, uint32_t n);

/* ---- write mode: only nand_write.c may call these (tests/check_gate_calls.sh) --------------------------------- */

/* Open the write window: WP# high, then t_ww (tWW). Write opcodes and data input panic outside it. */
void nand_bus_write_window_open(void);

/* Close it: WP# low (also aborts a running program/erase, §4.3). Call on every return path. */
void nand_bus_write_window_close(void);

/* n data-input cycles (Fig. 14/19): t_adl, then per byte data on IO, t_setup, WE# pulse t_wp, t_wh. Panics unless the
 * window is open and 80h + 5 address cycles came just before. */
void nand_bus_data_in(const uint8_t *buf, uint32_t n);

#endif
