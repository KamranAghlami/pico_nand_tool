/*
 * Pico NAND Tool firmware. Current scope: read-only NAND dumper (docs/SPEC.md).
 * M4: USB CDC transport, PING / SET_TIMING / ABORT, and the NAND commands RESET / READ_ID / READ_STATUS /
 * READ_PARAM / READ_PAGES (streamed).
 */
#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "nand_bus.h"
#include "pico/stdlib.h"
#include "protocol.h"
#include "tusb.h"

int main(void) {
    /* First: WP# low (write protected), then park the NAND control lines deasserted (CE#/WE#/RE# high, CLE/ALE low). */
    nand_bus_init();

    /* Fixed 125 MHz clk_sys: timing_t defaults assume 8 ns/cycle (docs/PROTOCOL.md). */
    set_sys_clock_khz(125000, true);

#ifdef PICO_DEFAULT_LED_PIN
    gpio_init(PICO_DEFAULT_LED_PIN);
    gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);
#endif

    protocol_init();
    nand_bus_use_timing(protocol_timing());
    tusb_init();

    for (;;) {
        tud_task();
        protocol_poll();
#ifdef PICO_DEFAULT_LED_PIN
        gpio_put(PICO_DEFAULT_LED_PIN, tud_mounted()); /* LED on = enumerated */
#endif
    }
}
