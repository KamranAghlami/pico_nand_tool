/* NAND bus primitives over RP2040 SIO (mask operations only). Datasheet sequences built on them: nand_ops.c. */
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

/* Back to the idle state from anywhere (panic handler): CE# high first, then CLE/ALE low, strobes high, IO input. */
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

/* Power-on wait (§4.1, Fig. 46): poll R/B# until high for at most timeout_us. No command precedes it, so no tWB. */
bool nand_bus_wait_rb_high(uint32_t timeout_us);

/* n data-output cycles (Fig. 15): IO to input, then per byte RE# low, t_rea, sample, RE# high, t_reh. Ends with
 * t_rhw, so the bus may be driven again right after. The caller has already waited t_whr or t_rr. */
void nand_bus_read(uint8_t *buf, uint32_t n);

#endif
