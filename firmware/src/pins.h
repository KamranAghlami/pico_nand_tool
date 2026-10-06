/*
 * The one and only pin map (docs/SPEC.md "Pin map"). Change pins here and nowhere else.
 * Ball numbers: datasheet Fig. 3 (63-BGA, x8).
 * WP# (ball C3) is on GP13 with a 10k external pull-down. Firmware only ever drives it LOW (= write protected, §2.5),
 * never high. tests/check_wp.sh fails the build on any other use than the drive-low init in nand_bus.c.
 *
 * What the signals do (docs/LEARNING_GUIDE.md §2.3). '#' = active low: the signal is "on" at 0 V.
 *   IO0-IO7  8-bit data bus, both directions. The Pico drives it for command/address/data-in cycles; the chip
 *            drives it while RE# is low (data out).
 *   CLE      high: the byte latched on WE#'s rising edge is a command opcode.
 *   ALE      high: the byte latched on WE#'s rising edge is an address byte. (Both low: data in.)
 *   WE#      write strobe. The chip captures IO on its rising edge.
 *   RE#      read strobe. Each low pulse makes the chip put the next byte on IO.
 *   CE#      chip enable. High: the chip ignores every other line, so it is the "safe" idle state.
 *   WP#      write protect. Low: the chip refuses program/erase. Raised only for one armed write operation.
 *   R/B#     ready/busy, driven by the chip (open drain, so it needs the pull-up). Low while the chip is busy.
 *
 * Why masks: the RP2040's SIO block has single-cycle GPIO_OUT_SET / _CLR / _XOR registers, each taking a 32-bit
 * mask of pins. gpio_set_mask(MASK_CE | MASK_WE) raises both pins with one store, at the same instant. The firmware
 * only ever uses these masks, never per-pin loops, which keeps every bus phase short and predictable.
 * IO0..IO7 on consecutive GPIOs means one read of GPIO_IN, shifted by PIN_IO0, is the data byte.
 */
#ifndef PINS_H
#define PINS_H

#define PIN_IO0 0   /* IO0..IO7 = GP0..GP7, balls H4 J4 K4 K5 K6 J7 K7 J8 (must be contiguous) */
#define PIN_CLE 8   /* ball D5 */
#define PIN_ALE 9   /* ball C4 */
#define PIN_WE 10   /* ball C7, WE#, 10k external pull-up */
#define PIN_RE 11   /* ball D4, RE#, 10k external pull-up */
#define PIN_CE 12   /* ball C6, CE#, 10k external pull-up */
#define PIN_WP 13   /* ball C3, WP#, 10k external pull-down; driven LOW only, never high */
#define PIN_RB 14   /* ball C8, R/B#, open drain, 10k external pull-up; input only */

#define MASK_IO (0xFFu << PIN_IO0)
#define MASK_CLE (1u << PIN_CLE)
#define MASK_ALE (1u << PIN_ALE)
#define MASK_WE (1u << PIN_WE)
#define MASK_RE (1u << PIN_RE)
#define MASK_CE (1u << PIN_CE)
#define MASK_WP (1u << PIN_WP)
#define MASK_RB (1u << PIN_RB)

/* Active-low strobes that idle high; active-high latches that idle low. */
#define MASK_CTRL_IDLE_HIGH (MASK_CE | MASK_WE | MASK_RE)
#define MASK_CTRL_IDLE_LOW (MASK_CLE | MASK_ALE)
#define MASK_ALL_NAND (MASK_IO | MASK_CTRL_IDLE_HIGH | MASK_CTRL_IDLE_LOW | MASK_RB)

/* _Static_assert is checked by the compiler, so a bad pin map can never be built at all (zero run-time cost). */
_Static_assert(PIN_IO0 + 7 <= 29, "IO0..IO7 must be 8 contiguous GPIOs");
_Static_assert((MASK_IO & (MASK_CTRL_IDLE_HIGH | MASK_CTRL_IDLE_LOW | MASK_RB)) == 0, "pin map overlap");
_Static_assert((MASK_CTRL_IDLE_HIGH & (MASK_CTRL_IDLE_LOW | MASK_RB)) == 0, "pin map overlap");
_Static_assert((MASK_CTRL_IDLE_LOW & MASK_RB) == 0, "pin map overlap");
/* WP# is outside every bus mask, so no bus operation (gpio_set_mask on MASK_CTRL_IDLE_HIGH, ...) can raise it. */
_Static_assert((MASK_WP & MASK_ALL_NAND) == 0, "WP# must not be part of any bus mask");

#endif
