#include "nand_bus.h"

#include "hardware/gpio.h"
#include "nand_cmd.h"
#include "pico/platform/panic.h"
#include "pins.h"

void nand_bus_init(void) {
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
}

void nand_bus_park(void) {
    gpio_set_mask(MASK_CE);                 /* deselect first: the chip now ignores WE#/RE#/CLE/ALE (Table 7) */
    gpio_clr_mask(MASK_CTRL_IDLE_LOW);      /* CLE, ALE low */
    gpio_set_mask(MASK_WE | MASK_RE);       /* strobes idle high */
    gpio_set_dir_in_masked(MASK_IO);        /* release the data bus */
}

void nand_bus_cmd_latch_(nand_cmd_t op) {
    /* Layer 2 of the opcode gate (nand_cmd.h): checked before anything touches the bus. */
    if (!nand_cmd_is_allowed((unsigned)op))
        panic("forbidden NAND opcode 0x%02x", (unsigned)op);

    /* The command latch cycle (Fig. 12) arrives with milestone M2. Until then nothing may reach the bus. */
    panic("NAND command latch not implemented before M2");
}
