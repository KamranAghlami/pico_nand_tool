/*
 * panic() handler (CMake: PICO_PANIC_FUNCTION=nand_tool_panic) and HardFault handler. Every panic, e.g. the opcode
 * gate's runtime check, and every HardFault ends here: interrupts off (USB stops answering, so the host times out),
 * NAND bus parked (WP# low first, which aborts a running program/erase, §4.3), watchdog stopped so the halt stays
 * visible, then a halt until power-cycle. Nothing is printed: there is no text channel (docs/PROTOCOL.md).
 */
#include "hardware/sync.h"
#include "hardware/watchdog.h"
#include "nand_bus.h"

void __attribute__((noreturn)) nand_tool_panic(const char *fmt, ...) {
    (void)fmt;
    (void)save_and_disable_interrupts();
    nand_bus_park();
    watchdog_disable();
    for (;;)
        __wfe();
}

/* Overrides the SDK's weak default (a breakpoint loop that would leave WP# as it was). */
void isr_hardfault(void) { nand_tool_panic(NULL); }
