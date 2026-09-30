/*
 * Pico NAND Tool firmware. Current scope: read-only NAND dumper (docs/SPEC.md).
 * M0: USB CDC transport + PING / SET_TIMING / ABORT; NAND lines held in their safe idle state.
 */
#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "nand_bus.h"
#include "pico/stdlib.h"
#include "protocol.h"
#include "tusb.h"

int main(void) {
    /* First: park the NAND control lines deasserted (CE#/WE#/RE# high, CLE/ALE low). */
    nand_bus_init();

    /* Fixed 125 MHz clk_sys: timing_t defaults assume 8 ns/cycle (docs/PROTOCOL.md). */
    set_sys_clock_khz(125000, true);

#ifdef PICO_DEFAULT_LED_PIN
    gpio_init(PICO_DEFAULT_LED_PIN);
    gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);
#endif

    protocol_init();
    tusb_init();

    for (;;) {
        tud_task();
        protocol_poll();
#ifdef PICO_DEFAULT_LED_PIN
        gpio_put(PICO_DEFAULT_LED_PIN, tud_mounted()); /* LED on = enumerated */
#endif
    }
}
