/*
 * panic() handler (CMake: PICO_PANIC_FUNCTION=nand_tool_panic) and HardFault handler. Every panic, e.g. the opcode
 * gate's runtime check, and every HardFault ends here: interrupts off (USB stops answering, so the host times out),
 * NAND bus parked (WP# low first, which aborts a running program/erase, §4.3), watchdog stopped so the halt stays
 * visible, then a halt until power-cycle. Nothing is printed: there is no text channel (docs/PROTOCOL.md).
 *
 * Design idea: "fail safe". When the firmware no longer trusts its own state, the one thing that must still happen
 * is that the chip ends up write-protected and deselected. Everything else (USB, telling the host why) is
 * secondary. The host notices the silence (timeouts) and the user power-cycles. __wfe() ("wait for event") just
 * sleeps the core instead of spinning.
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
