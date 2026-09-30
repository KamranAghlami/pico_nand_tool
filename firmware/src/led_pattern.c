#include "led_pattern.h"

/* ms timestamps wrap after ~49 days; compare with signed differences. */
static bool before(uint32_t a, uint32_t b) { return (int32_t)(a - b) < 0; }

static bool active(const led_state_t *s, uint32_t now) {
    return s->activity_seen && before(now, s->activity_ms + LED_ACTIVITY_HOLD_MS);
}

void led_pattern_note_activity(led_state_t *s, uint32_t now) {
    if (!active(s, now))
        s->flicker_start_ms = now; /* new burst: flicker starts with an "off" so even one event is visible */
    s->activity_ms = now;
    s->activity_seen = true;
}

void led_pattern_note_error(led_state_t *s, uint32_t now) {
    s->error_until_ms = now + LED_ERROR_SHOW_MS;
    s->error_seen = true;
}

bool led_pattern_status(const led_state_t *s, uint32_t now, bool flicker_on_status) {
    if (s->error_seen && before(now, s->error_until_ms))
        return (now / 50) % 2 == 0; /* 10 Hz */
    switch (s->link) {
    case LED_LINK_NONE:
        return now % 1000 < 500; /* 1 Hz */
    case LED_LINK_USB:
        return now % 2000 < 60; /* heartbeat blip */
    case LED_LINK_HOST:
    default:
        if (flicker_on_status && active(s, now))
            return ((now - s->flicker_start_ms) / LED_FLICKER_HALF_MS) % 2 == 1; /* off first */
        return true;
    }
}

bool led_pattern_activity(const led_state_t *s, uint32_t now) { return active(s, now); }

bool led_pattern_fault(uint32_t t) {
    const uint32_t period = 6 * LED_FAULT_BLINK_MS + LED_FAULT_PAUSE_MS;
    uint32_t p = t % period;
    return p < 6 * LED_FAULT_BLINK_MS && (p / LED_FAULT_BLINK_MS) % 2 == 0;
}
