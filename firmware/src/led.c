#include "led.h"

#include "hardware/gpio.h"
#include "hardware/timer.h"
#include "pico/time.h"
#include "pins.h"

#define LED_REFRESH_MS 10

static led_state_t state;
static repeating_timer_t refresh_timer;

static uint32_t now_ms(void) { return to_ms_since_boot(get_absolute_time()); }

static void led_out_init(unsigned pin) {
    gpio_init(pin);
    gpio_put(pin, 0);
    gpio_set_dir(pin, GPIO_OUT);
}

/* Timer IRQ. The IRQ only ever lengthens a NAND bus phase, which is safe: all host-side limits are minimums. */
static bool refresh(repeating_timer_t *rt) {
    (void)rt;
    uint32_t t = now_ms();
#if PIN_LED_STATUS >= 0
    gpio_put(PIN_LED_STATUS, led_pattern_status(&state, t, PIN_LED_ACTIVITY < 0));
#endif
#if PIN_LED_ACTIVITY >= 0
    gpio_put(PIN_LED_ACTIVITY, led_pattern_activity(&state, t));
#endif
    return true;
}

void led_init(void) {
#if PIN_LED_STATUS >= 0
    led_out_init(PIN_LED_STATUS);
#endif
#if PIN_LED_ACTIVITY >= 0
    led_out_init(PIN_LED_ACTIVITY);
#endif
    state.link = LED_LINK_NONE;
    add_repeating_timer_ms(-LED_REFRESH_MS, refresh, NULL, &refresh_timer);
}

void led_set_link(led_link_t link) { state.link = link; }

void led_activity(void) { led_pattern_note_activity(&state, now_ms()); }

void led_error(void) { led_pattern_note_error(&state, now_ms()); }

void led_fault_forever(void) {
    /* (Re)configure the pins: the fault may have happened before led_init(). */
#if PIN_LED_STATUS >= 0
    led_out_init(PIN_LED_STATUS);
#endif
#if PIN_LED_ACTIVITY >= 0
    led_out_init(PIN_LED_ACTIVITY);
#endif
    uint64_t start = time_us_64(); /* the timer keeps counting with interrupts disabled */
    for (;;) {
        bool on = led_pattern_fault((uint32_t)((time_us_64() - start) / 1000));
#if PIN_LED_STATUS >= 0
        gpio_put(PIN_LED_STATUS, on);
#endif
#if PIN_LED_ACTIVITY >= 0
        gpio_put(PIN_LED_ACTIVITY, on);
#endif
    }
}
