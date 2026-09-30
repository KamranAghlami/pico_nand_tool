/*
 * panic() handler (CMake: PICO_PANIC_FUNCTION=nand_tool_panic). Every panic, e.g. the opcode gate's runtime check,
 * ends here: interrupts off (USB stops answering), NAND bus parked in its idle state, LED shows the FAULT pattern
 * until power-cycle. Nothing is printed: there is no text channel (docs/PROTOCOL.md).
 */
#include "hardware/sync.h"
#include "led.h"
#include "nand_bus.h"

void __attribute__((noreturn)) nand_tool_panic(const char *fmt, ...) {
    (void)fmt;
    (void)save_and_disable_interrupts();
    nand_bus_park();
    led_fault_forever();
}
