/*
 * Bit-banged NAND bus: the only code that moves the NAND pins (contracts: nand_bus.h).
 *
 * Study notes
 *  - "Bit-banging": the RP2040 has no NAND controller, so every bus edge is a store to a GPIO register followed by a
 *    busy-wait. One page read is 2112 RE# pulses, i.e. a few thousand stores and spin loops.
 *  - __not_in_flash_func(f) / __not_in_flash("nand") put the function in RAM. Normally code runs from the external
 *    QSPI flash through a small cache, and a cache miss stalls the CPU while the flash is fetched. That would stretch
 *    bus phases unpredictably (always safe here, since every delay is a minimum, but slow and jittery).
 *  - gpio_set_mask / gpio_clr_mask write the SIO GPIO_OUT_SET / GPIO_OUT_CLR registers: every pin in the mask changes
 *    with one store, at the same instant. gpio_put_masked(mask, value) sets the pins in `mask` to the bits of `value`
 *    (also one store, via GPIO_OUT_XOR), which is how a byte and CLE/ALE go out together.
 *  - gpio_set_dir_out_masked / gpio_set_dir_in_masked turn the output drivers on and off. The IO bus is shared: the
 *    Pico drives it for command/address/data-in cycles and must let go (input) before the chip drives it (RE# low).
 *    Two drivers fighting over a wire is a short circuit, hence the careful tWHR / tRHW / tCHZ waits.
 *  - Write-mode sequence state machine (wseq below), advanced by the command latch and address functions:
 *
 *        NONE --80h--> PROG_ADDR --5th address--> PROG_DATA --data input--> PROG_LOADED --10h--> NONE
 *        NONE --60h--> ERASE_ADDR --3rd address--> ERASE_READY --D0h--> NONE
 *
 *    Any other command, or an extra address cycle, drops back to NONE, so a confirm opcode (10h/D0h) or a data-input
 *    cycle out of order finds the wrong state and panics before touching the bus.
 *  - panic() is the SDK's, redirected by CMake (PICO_PANIC_FUNCTION) to nand_tool_panic() in fault.c: park the bus
 *    with WP# low and halt. A panic here means a firmware bug; stopping is safer than carrying on.
 */
#include "nand_bus.h"

#include <stddef.h>

#include "hardware/gpio.h"
#include "hardware/structs/systick.h"
#include "hardware/timer.h"
#include "nand_cmd.h"
#include "pico/platform.h"
#include "pico/platform/panic.h"
#include "pins.h"

/* File-private state. Everything runs from the single main loop (no threads, no NAND work in interrupts), so plain
 * statics need no locking. tm points at protocol.c's timing_t, so SET_TIMING takes effect on the next bus cycle. */
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

/* Every phase is a minimum (timing.h): interrupts or extra instructions only make it longer, which is always safe.
 * busy_wait_at_least_cycles() (Pico SDK) spins the CPU for at least that many clk_sys cycles. __force_inline makes
 * sure no function call (and no flash-resident code) sits in the middle of a bus phase. */
static __force_inline void delay(uint32_t cycles) { busy_wait_at_least_cycles(cycles); }

/* SysTick runs free at clk_sys (24 bit, wraps after 134 ms at 125 MHz, longer than any R/B# timeout). Down-counter.
 * It is used only to *measure* R/B# busy time (reported to the host). Because it counts down, elapsed = start - now;
 * the & 0xFFFFFF makes that subtraction modulo 2^24, so it stays right across one wrap-around (e.g. start = 5,
 * now = 0xFFFFFE after wrapping: (5 - 0xFFFFFE) & 0xFFFFFF = 7 cycles). */
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

    /* SysTick: processor clock, no interrupt, full 24-bit reload (R/B# busy-time measurement). These are the ARM
     * Cortex-M0+ core's own SysTick registers: rvr = reload value, cvr = current value, csr = control/status. */
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
 * with t_rhw (tRHW), the last deselect with t_ceh (tCHZ).
 *
 *   CLE or ALE  ____/---------------------------------\____
 *   IO          ----< v (driven by the Pico) >-------------   (stays driven afterwards)
 *   WE#         -------------\__________/-------------
 *                   | t_setup |   t_wp   |   t_wh    |
 *                                       ^ chip latches v here, on WE# rising
 */
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
        /* Setup opcodes (80h, 60h) may only start a fresh sequence; each confirm opcode needs its own setup to be
         * complete: 10h after the data was loaded, D0h after the 3 erase address cycles. */
        bool in_order = (op == NAND_CMD_PROGRAM_1 || op == NAND_CMD_ERASE_1) ? wseq == WSEQ_NONE
                      : op == NAND_CMD_PROGRAM_2                           ? wseq == WSEQ_PROG_LOADED
                                                                           : wseq == WSEQ_ERASE_READY;
        if (!in_order)
            panic("NAND write opcode 0x%02x out of sequence", (unsigned)op);
    }
    latch_cycle(MASK_CLE, (uint8_t)op); /* Fig. 12 */
    /* Advance the state machine (see the top of this file). Every read-side opcode resets it to NONE. */
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

/* Spin until R/B# reads high, or give up after timeout_us (the firmware never hangs on a dead or missing chip).
 * Two clocks: time_us_32() (the SDK's 1 MHz timer) for the coarse timeout, SysTick for a cycle-exact busy time.
 * time_us_32() - t0 is unsigned, so it stays correct even if the 32-bit microsecond counter wraps. */
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

/* Data-out cycles: the chip drives IO, the Pico samples it.
 *
 *   RE#   -----\__________/------\__________/------ ...
 *               |  t_rea   | t_reh|
 *                         ^ sample IO (gpio_get_all) just before RE# rises
 *
 * gpio_get_all() returns all 30 GPIO input levels in one word; >> PIN_IO0 and the (uint8_t) cast keep IO0..IO7. */
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
