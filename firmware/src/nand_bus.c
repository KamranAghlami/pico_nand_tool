#include "nand_bus.h"

#include <stddef.h>

#include "hardware/gpio.h"
#include "hardware/structs/systick.h"
#include "hardware/timer.h"
#include "nand_cmd.h"
#include "pico/platform.h"
#include "pico/platform/panic.h"
#include "pins.h"

static const timing_t *tm;

/* Write mode state (layer 2 of the opcode gate, nand_cmd.h). The window is open, with WP# high, for exactly one
 * erase or program operation (nand_write.c). wseq tracks the sequence so 10h / D0h / data input can only follow a
 * complete 80h or 60h setup. */
static bool write_window;
static enum {
    WSEQ_NONE,
    WSEQ_PROG_ADDR,   /* 80h latched, address cycles counting  */
    WSEQ_PROG_DATA,   /* 80h + 5 addresses: data input allowed */
    WSEQ_PROG_LOADED, /* data input done: 10h allowed          */
    WSEQ_ERASE_ADDR,  /* 60h latched, address cycles counting  */
    WSEQ_ERASE_READY, /* 60h + 3 addresses: D0h allowed        */
} wseq;
static uint8_t waddr; /* address cycles since 80h / 60h */

/* Every phase is a minimum (timing.h): interrupts or extra instructions only make it longer, which is always safe. */
static __force_inline void delay(uint32_t cycles) { busy_wait_at_least_cycles(cycles); }

/* SysTick runs free at clk_sys (24 bit, wraps after 134 ms at 125 MHz, longer than any R/B# timeout). Down-counter. */
static __force_inline uint32_t cycles_now(void) { return systick_hw->cvr; }
static __force_inline uint32_t cycles_since(uint32_t start) { return (start - cycles_now()) & 0xFFFFFFu; }

void nand_bus_init(void) {
    /* WP# low = hardware write protection (§2.5). The 10k external pull-down already holds it low; drive it low too.
     * gpio_init_mask leaves the pin an input with output latch 0, so the latch is low before the driver is enabled.
     * These three lines are the only WP# code allowed (tests/check_wp.sh); it is never set high. */
    gpio_init_mask(MASK_WP);
    gpio_clr_mask(MASK_WP);
    gpio_set_dir_out_masked(MASK_WP);

    /* RP2040 pads reset with internal pull-downs, which fight the 10k external pull-ups on CE#/WE#/RE# and R/B#
     * (PROPOSAL §1 item 7). Release them first so those lines sit firmly high. */
    for (unsigned pin = 0; pin < 30; pin++)
        if ((MASK_CTRL_IDLE_HIGH | MASK_RB) & (1u << pin))
            gpio_disable_pulls(pin);

    /* SIO function, all inputs, output latches 0: nothing is driven yet. */
    gpio_init_mask(MASK_ALL_NAND);

    /* Latch the deasserted levels BEFORE enabling the drivers (SPEC "On init, drive CE#/WE#/RE# high before
     * enabling them as outputs"). CLE/ALE low = no latch. */
    gpio_set_mask(MASK_CTRL_IDLE_HIGH);
    gpio_clr_mask(MASK_CTRL_IDLE_LOW | MASK_IO);
    gpio_set_dir_out_masked(MASK_CTRL_IDLE_HIGH | MASK_CTRL_IDLE_LOW);

    /* IO stays an input with the default pull-downs (defined level while the chip is deselected);
     * R/B# stays an input on its external pull-up. */

    /* SysTick: processor clock, no interrupt, full 24-bit reload (R/B# busy-time measurement). */
    systick_hw->rvr = 0xFFFFFFu;
    systick_hw->cvr = 0;
    systick_hw->csr = 0x5; /* CLKSOURCE = processor clock, ENABLE */
}

void nand_bus_use_timing(const timing_t *t) { tm = t; }

void __not_in_flash_func(nand_bus_park)(void) {
    gpio_clr_mask(MASK_WP);            /* write protect first: aborts a running program/erase (§4.3) */
    write_window = false;
    gpio_set_mask(MASK_CE);            /* deselect: the chip now ignores WE#/RE#/CLE/ALE (Table 3) */
    gpio_clr_mask(MASK_CTRL_IDLE_LOW); /* CLE, ALE low */
    gpio_set_mask(MASK_WE | MASK_RE);  /* strobes idle high */
    gpio_set_dir_in_masked(MASK_IO);   /* release the data bus */
}

void __not_in_flash_func(nand_bus_select)(void) {
    gpio_clr_mask(MASK_CE);
    delay(tm->t_cs); /* tCS, tCR */
}

void __not_in_flash_func(nand_bus_deselect)(void) {
    gpio_set_dir_in_masked(MASK_IO);
    gpio_set_mask(MASK_CE);
    delay(tm->t_ceh); /* tCHZ: the chip releases IO; tCSD before the next CE# low */
}

/* One latch cycle with CLE (command, Fig. 12) or ALE (address, Fig. 13) high. Never anything else: WE# with
 * CLE = ALE = 0 is data input, which exists only in the program-only data-input function below (SPEC "Hard safety
 * constraints"). Callers pass constants, so the check folds away. The bus may be driven here: the last read ended
 * with t_rhw (tRHW), the last deselect with t_ceh (tCHZ). */
static __force_inline void latch_cycle(uint32_t ctrl, uint8_t v) {
    if (ctrl != MASK_CLE && ctrl != MASK_ALE)
        panic("NAND latch without CLE/ALE");
    gpio_put_masked(MASK_IO | MASK_CTRL_IDLE_LOW, ctrl | ((uint32_t)v << PIN_IO0));
    gpio_set_dir_out_masked(MASK_IO);
    delay(tm->t_setup); /* tCLS / tALS / tDS (to WE# high, with t_wp) */
    gpio_clr_mask(MASK_WE);
    delay(tm->t_wp); /* tWP */
    gpio_set_mask(MASK_WE);
    delay(tm->t_wh); /* tCLH / tALH / tDH / tCH; with t_setup: tWH */
    gpio_clr_mask(MASK_CTRL_IDLE_LOW);
}

__not_in_flash("nand")
void nand_bus_cmd_latch_(nand_cmd_t op) {
    /* Layer 2 of the opcode gate (nand_cmd.h): checked before anything touches the bus. */
    if (!nand_cmd_is_allowed((unsigned)op))
        panic("forbidden NAND opcode 0x%02x", (unsigned)op);
    if (nand_cmd_is_write((unsigned)op)) {
        if (!write_window)
            panic("NAND write opcode 0x%02x outside the write window", (unsigned)op);
        bool in_order = (op == NAND_CMD_PROGRAM_1 || op == NAND_CMD_ERASE_1) ? wseq == WSEQ_NONE
                      : op == NAND_CMD_PROGRAM_2                           ? wseq == WSEQ_PROG_LOADED
                                                                           : wseq == WSEQ_ERASE_READY;
        if (!in_order)
            panic("NAND write opcode 0x%02x out of sequence", (unsigned)op);
    }
    latch_cycle(MASK_CLE, (uint8_t)op); /* Fig. 12 */
    wseq = op == NAND_CMD_PROGRAM_1 ? WSEQ_PROG_ADDR : op == NAND_CMD_ERASE_1 ? WSEQ_ERASE_ADDR : WSEQ_NONE;
    waddr = 0;
}

void __not_in_flash_func(nand_bus_addr)(uint8_t a) {
    latch_cycle(MASK_ALE, a); /* Fig. 13 */
    if (wseq == WSEQ_PROG_ADDR && ++waddr == 5)
        wseq = WSEQ_PROG_DATA; /* §3.2: 5 address cycles */
    else if (wseq == WSEQ_ERASE_ADDR && ++waddr == 3)
        wseq = WSEQ_ERASE_READY; /* §3.5: 3 row address cycles */
    else if (wseq != WSEQ_PROG_ADDR && wseq != WSEQ_ERASE_ADDR)
        wseq = WSEQ_NONE; /* an extra address cycle breaks the sequence: 10h / D0h / data input will panic */
}

void __not_in_flash_func(nand_bus_write_window_open)(void) {
    write_window = true;
    wseq = WSEQ_NONE;
    gpio_set_mask(MASK_WP); /* WP# HIGH: the only place (check_wp.sh). Lowered by write_window_close / park. */
    delay(tm->t_ww);        /* tWW: WP# high before WE# rises for 80h / 60h (§4.3, Fig. 47/48) */
}

void __not_in_flash_func(nand_bus_write_window_close)(void) {
    gpio_clr_mask(MASK_WP); /* WP# low again; aborts a still-running program/erase (§4.3) */
    write_window = false;
    wseq = WSEQ_NONE;
}

void __not_in_flash_func(nand_bus_data_in)(const uint8_t *buf, uint32_t n) {
    if (!write_window || wseq != WSEQ_PROG_DATA)
        panic("NAND data input outside a program sequence");
    /* §3.2, Fig. 19 (Serial Data Input cycle, Fig. 14): CLE = ALE = 0 (the last latch cycle cleared them), data on
     * IO, WE# pulse. tADL counts from the last address WE# high to the first data WE# high; t_setup + t_wp add to
     * it. */
    delay(tm->t_adl);
    gpio_set_dir_out_masked(MASK_IO);
    for (uint32_t i = 0; i < n; i++) {
        gpio_put_masked(MASK_IO, (uint32_t)buf[i] << PIN_IO0);
        delay(tm->t_setup); /* tDS (with t_wp) */
        gpio_clr_mask(MASK_WE);
        delay(tm->t_wp); /* tWP */
        gpio_set_mask(MASK_WE);
        delay(tm->t_wh); /* tDH; with t_setup: tWH; all three: tWC */
    }
    wseq = WSEQ_PROG_LOADED;
}

void __not_in_flash_func(nand_bus_wait_whr)(void) { delay(tm->t_whr); /* tWHR 60 covers tAR 10, tCLR 10 */ }

static bool __not_in_flash_func(poll_rb_high)(uint32_t timeout_us, uint32_t *busy_cycles) {
    uint32_t c0 = cycles_now();
    uint32_t t0 = time_us_32();
    bool seen_low = false;
    while (!(gpio_get_all() & MASK_RB)) {
        seen_low = true;
        if (time_us_32() - t0 > timeout_us)
            return false;
    }
    if (busy_cycles)
        *busy_cycles = seen_low ? cycles_since(c0) : 0;
    return true;
}

bool __not_in_flash_func(nand_bus_wait_ready)(uint32_t *busy_cycles) {
    return nand_bus_wait_ready_us(tm->rb_timeout_us, busy_cycles);
}

bool __not_in_flash_func(nand_bus_wait_ready_us)(uint32_t timeout_us, uint32_t *busy_cycles) {
    uint32_t c0 = cycles_now();
    delay(tm->t_wb); /* tWB: R/B# is not valid until then */
    uint32_t polled;
    if (!poll_rb_high(timeout_us, &polled))
        return false;
    if (busy_cycles)
        *busy_cycles = polled ? cycles_since(c0) : 0;
    delay(tm->t_rr); /* tRR */
    return true;
}

bool __not_in_flash_func(nand_bus_wait_rb_high)(uint32_t timeout_us) { return poll_rb_high(timeout_us, NULL); }

void __not_in_flash_func(nand_bus_read)(uint8_t *buf, uint32_t n) {
    gpio_set_dir_in_masked(MASK_IO); /* before the first RE# fall (SPEC "Firmware requirements") */
    for (uint32_t i = 0; i < n; i++) {
        gpio_clr_mask(MASK_RE);
        delay(tm->t_rea); /* tREA + 2-cycle input synchronizer; tRP */
        buf[i] = (uint8_t)(gpio_get_all() >> PIN_IO0);
        gpio_set_mask(MASK_RE);
        delay(tm->t_reh); /* tREH; with t_rea: tRC */
    }
    delay(tm->t_rhw); /* tRHW, tRHZ: the chip has released IO before anyone drives it */
}
