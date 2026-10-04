#include "nand_ops.h"

#include <stddef.h>

#include "nand_bus.h"
#include "nand_cmd.h"
#include "protocol_defs.h"

/* §4.1, Fig. 46: R/B# returns high within 5 ms of power-up. 2x margin. */
#define NAND_POWER_ON_TIMEOUT_US 10000

uint8_t nand_reset(uint32_t *busy_cycles) {
    /* §4.1: during the power-on busy time the chip ignores every command except 70h, so FFh must wait for it.
     * Harmless when the chip is long powered up: R/B# is already high. */
    if (!nand_bus_wait_rb_high(NAND_POWER_ON_TIMEOUT_US))
        return PROTO_ST_ERR_RB_TIMEOUT;

    /* §3.12, Fig. 33: FFh, R/B# low for tRST (≤ 5 µs from ready, Table 20), SR = 60h afterwards with WP# low. */
    nand_bus_select();
    NAND_CMD(NAND_CMD_RESET);
    bool ready = nand_bus_wait_ready(busy_cycles);
    nand_bus_deselect();
    return ready ? PROTO_ST_OK : PROTO_ST_ERR_RB_TIMEOUT;
}

uint8_t nand_read_id(uint8_t addr, uint8_t *buf, uint32_t n) {
    /* §3.16, Fig. 41 (addr 00h: 01 DA 90 95 44, Table 14) / §3.18, Fig. 43 (addr 20h: "ONFI") */
    nand_bus_select();
    NAND_CMD(NAND_CMD_READ_ID);
    nand_bus_addr(addr);
    nand_bus_wait_whr(); /* tWHR / tAR, Fig. 41 */
    nand_bus_read(buf, n);
    nand_bus_deselect();
    return PROTO_ST_OK;
}

uint8_t nand_read_status(uint8_t *sr) {
    /* §3.16 note: after Read ID, a dummy 00h must precede 70h. Always sending it means no hidden "what came before"
     * state (PROPOSAL §1 item 2). */
    nand_bus_select();
    NAND_CMD(NAND_CMD_READ_1);
    NAND_CMD(NAND_CMD_READ_STATUS); /* §3.9, Fig. 31 */
    nand_bus_wait_whr();            /* tWHR, tCLR */
    nand_bus_read(sr, 1);
    nand_bus_deselect();
    return PROTO_ST_OK;
}

uint8_t nand_read_param(uint8_t *buf, uint32_t n) {
    /* §3.19 note: without a Reset first, the 41 nm 2 Gb part can return wrong values (00h). */
    uint8_t st = nand_reset(NULL);
    if (st != PROTO_ST_OK)
        return st;

    /* §3.19, Fig. 44: ECh, 00h, R/B# low for tR (≤ 25 µs, Table 3.4 bytes 137-138), then data out. R/B# is polled, not
     * the status register, so no extra 00h is needed (Fig. 44 note 78). */
    nand_bus_select();
    NAND_CMD(NAND_CMD_READ_PARAM);
    nand_bus_addr(0x00);
    bool ready = nand_bus_wait_ready(NULL); /* tWB, tR, tRR */
    if (ready)
        nand_bus_read(buf, n);
    nand_bus_deselect();
    return ready ? PROTO_ST_OK : PROTO_ST_ERR_RB_TIMEOUT;
}

uint8_t nand_read_page(uint32_t page, uint8_t *buf, uint32_t n) {
    /* §3.1, Fig. 6.1. Table 5 (2 Gb, x8): column = 2 cycles (CA0-CA11; 0 = start of page), row = 3 cycles, LSB first:
     * PA0-PA5 page in block, PLA0 plane, BA0-BA9 block. Together that is just the page index 0..131071. */
    nand_bus_select();
    NAND_CMD(NAND_CMD_READ_1);
    nand_bus_addr(0x00);                  /* Col. Add. 1 */
    nand_bus_addr(0x00);                  /* Col. Add. 2 */
    nand_bus_addr((uint8_t)page);         /* Row Add. 1: PA0-PA5, PLA0, BA0 */
    nand_bus_addr((uint8_t)(page >> 8));  /* Row Add. 2: BA1-BA8 */
    nand_bus_addr((uint8_t)(page >> 16)); /* Row Add. 3: BA9, rest low */
    NAND_CMD(NAND_CMD_READ_2);
    bool ready = nand_bus_wait_ready(NULL); /* tWB, tR (≤ 25 µs), tRR */
    if (ready)
        nand_bus_read(buf, n);
    nand_bus_deselect();
    return ready ? PROTO_ST_OK : PROTO_ST_ERR_RB_TIMEOUT;
}
