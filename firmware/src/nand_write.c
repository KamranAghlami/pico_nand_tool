/*
 * Write mode: the arm state, Block Erase and Page Program (contracts: nand_write.h).
 *
 * Study notes
 *  - This is the only file allowed to use the write path (NAND_WCMD, nand_bus_write_window_open/close,
 *    nand_bus_data_in); tests/check_gate_calls.sh enforces it. So everything that can change the chip is in here.
 *  - Two independent permissions must both be given for a write to reach the chip:
 *      1. the arm (software): the host sent ARM_WRITE with the magic token for a block range, recently enough;
 *      2. the write window (hardware): WP# high, opened here for exactly one erase or program and closed after it.
 *  - Order of checks in each operation: arguments -> armed for this block -> (erase) bad-block markers -> bus.
 *    Every refusal happens before the bus is touched.
 *  - Program and erase are not "fire and forget": afterwards the status register says whether they worked
 *    (finish() below). A failed or timed-out operation disarms, so the host has to arm again deliberately.
 */
#include "nand_write.h"

#include "nand_bus.h"
#include "nand_cmd.h"
#include "nand_ops.h"
#include "pico/time.h"
#include "protocol_defs.h"

/* RAM only: every boot starts disarmed. */
static struct {
    bool armed;
    uint16_t first, last;
    uint64_t idle_us;
    uint64_t last_use_us; /* ARM_WRITE or the last successful erase/program */
} arm;

uint8_t nand_write_arm(uint32_t token, uint16_t first_block, uint16_t last_block, uint16_t idle_timeout_s) {
    if (token != PROTO_ARM_TOKEN || first_block > last_block || last_block >= PROTO_BLOCKS || idle_timeout_s < 1 ||
        idle_timeout_s > PROTO_ARM_IDLE_MAX_S)
        return PROTO_ST_ERR_BAD_ARGS;
    arm.first = first_block;
    arm.last = last_block;
    arm.idle_us = (uint64_t)idle_timeout_s * 1000000u;
    arm.last_use_us = time_us_64();
    arm.armed = true;
    return PROTO_ST_OK;
}

void nand_write_disarm(void) { arm.armed = false; }

/* The idle timeout is checked lazily, here, at the moment of use: there is no timer interrupt that disarms. That is
 * enough, since nothing can be erased or programmed without passing through this check first. */
static bool armed_for(uint16_t block) {
    if (arm.armed && time_us_64() - arm.last_use_us > arm.idle_us)
        arm.armed = false; /* idle timeout */
    return arm.armed && block >= arm.first && block <= arm.last;
}

/* Common tail after the confirm command (10h / D0h): wait for R/B#, read SR while WP# is still high (so bit 7 shows
 * whether it really was), close the window. Any failure disarms. */
static uint8_t finish(uint32_t timeout_us, uint8_t *sr, uint32_t *busy_cycles) {
    bool ready = nand_bus_wait_ready_us(timeout_us, busy_cycles); /* tWB, tPROG / tBERS, tRR */
    if (ready) {
        NAND_CMD(NAND_CMD_READ_STATUS); /* §3.9, Fig. 31 */
        nand_bus_wait_whr();            /* tWHR */
        nand_bus_read(sr, 1);
    }
    nand_bus_write_window_close(); /* WP# low. On a timeout this aborts the operation, like FFh (§4.3) */
    nand_bus_deselect();

    if (!ready) {
        arm.armed = false;
        busy_wait_us(1);        /* §4.3: WP# low for about 100 ns aborts the operation */
        (void)nand_reset(NULL); /* waits out tRST (≤ 500 µs from erase busy, Table 20), then FFh */
        return PROTO_ST_ERR_RB_TIMEOUT;
    }
    /* Table 13: bit 7 = 0 means WP# was low, so the chip ignored the command (§2.5); bit 6 = ready; bit 0 = fail.
     * (*sr & 0x41) != 0x40 means "not (ready and passed)": a healthy result is E0h (WP# high, ready, idle, pass). */
    uint8_t st = !(*sr & 0x80)          ? PROTO_ST_ERR_WP_STUCK
               : (*sr & 0x41) != 0x40   ? PROTO_ST_ERR_OP_FAILED
                                        : PROTO_ST_OK;
    if (st == PROTO_ST_OK)
        arm.last_use_us = time_us_64();
    else
        arm.armed = false;
    return st;
}

uint8_t nand_erase_block(uint16_t block, bool ignore_bad_marker, uint8_t *sr, uint32_t *busy_cycles) {
    if (block >= PROTO_BLOCKS)
        return PROTO_ST_ERR_BAD_ARGS;
    if (!armed_for(block))
        return PROTO_ST_ERR_NOT_ARMED;
    uint32_t first_page = (uint32_t)block * PROTO_PAGES_PER_BLOCK;

    /* §9.2, Fig. 55 note 84: read the bad-block information before any erase, because an erase may destroy it. */
    if (!ignore_bad_marker) {
        static const uint8_t marker_pages[] = {0, 1, PROTO_PAGES_PER_BLOCK - 1};
        for (unsigned i = 0; i < sizeof marker_pages; i++) {
            uint8_t m;
            uint8_t st = nand_read_column(first_page + marker_pages[i], 2048, &m, 1); /* spare byte 0 */
            if (st != PROTO_ST_OK) {
                arm.armed = false;
                return st;
            }
            if (m != 0xFF)
                return PROTO_ST_ERR_BAD_BLOCK;
        }
    }

    /* §3.5, Fig. 24: 60h, 3 row address cycles (only the block bits count; page bits are ignored), D0h. */
    nand_bus_select();
    nand_bus_write_window_open(); /* WP# high, tWW (Fig. 48) */
    NAND_WCMD(NAND_CMD_ERASE_1);
    nand_bus_addr((uint8_t)first_page);         /* Row Add. 1: PA0-PA5 (0), PLA0, BA0 */
    nand_bus_addr((uint8_t)(first_page >> 8));  /* Row Add. 2: BA1-BA8 */
    nand_bus_addr((uint8_t)(first_page >> 16)); /* Row Add. 3: BA9 */
    NAND_WCMD(NAND_CMD_ERASE_2);
    return finish(PROTO_ERASE_TIMEOUT_US, sr, busy_cycles);
}

uint8_t nand_program_page(uint32_t page, const uint8_t *data, uint8_t *sr, uint32_t *busy_cycles) {
    if (page >= PROTO_TOTAL_PAGES)
        return PROTO_ST_ERR_BAD_ARGS;
    if (!armed_for((uint16_t)(page / PROTO_PAGES_PER_BLOCK)))
        return PROTO_ST_ERR_NOT_ARMED;

    /* §3.2, Fig. 19: 80h, 5 address cycles (column 0, row = page; Table 5), 2112 data bytes, 10h. */
    nand_bus_select();
    nand_bus_write_window_open(); /* WP# high, tWW (Fig. 47) */
    NAND_WCMD(NAND_CMD_PROGRAM_1);
    nand_bus_addr(0x00);                  /* Col. Add. 1 */
    nand_bus_addr(0x00);                  /* Col. Add. 2 */
    nand_bus_addr((uint8_t)page);         /* Row Add. 1: PA0-PA5, PLA0, BA0 */
    nand_bus_addr((uint8_t)(page >> 8));  /* Row Add. 2: BA1-BA8 */
    nand_bus_addr((uint8_t)(page >> 16)); /* Row Add. 3: BA9 */
    nand_bus_data_in(data, PROTO_PAGE_LEN); /* tADL, then 2112 data input cycles into the chip's page register */
    /* 10h starts the actual programming: the chip copies the page register into the array (tPROG, ~200 µs). It can
     * only clear bits (1 -> 0), which is why the host only programs pages it has just erased. */
    NAND_WCMD(NAND_CMD_PROGRAM_2);
    return finish(PROTO_PROGRAM_TIMEOUT_US, sr, busy_cycles);
}
