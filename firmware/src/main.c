/*
 * Pico NAND Tool firmware. Current scope: read-only NAND dumper (docs/SPEC.md).
 * M0: USB CDC transport + PING / SET_TIMING / ABORT; NAND lines held in their safe idle state.
 */
#include "hardware/clocks.h"
#include "led.h"
#include "nand_bus.h"
#include "pico/stdlib.h"
#include "protocol.h"
#include "tusb.h"

/* USB device state -> status LED (DTR changes are handled in protocol.c tud_cdc_line_state_cb). */
void tud_mount_cb(void) { led_set_link(tud_cdc_connected() ? LED_LINK_HOST : LED_LINK_USB); }
void tud_umount_cb(void) { led_set_link(LED_LINK_NONE); }
void tud_suspend_cb(bool remote_wakeup_en) {
    (void)remote_wakeup_en;
    led_set_link(LED_LINK_NONE);
}
void tud_resume_cb(void) { tud_mount_cb(); }

int main(void) {
    /* First: park the NAND control lines deasserted (CE#/WE#/RE# high, CLE/ALE low). */
    nand_bus_init();

    /* Fixed 125 MHz clk_sys: timing_t defaults assume 8 ns/cycle (docs/PROTOCOL.md). */
    set_sys_clock_khz(125000, true);

    led_init();
    protocol_init();
    tusb_init();

    for (;;) {
        tud_task();
        protocol_poll();
    }
}
