#include "nand_ops.h"

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
