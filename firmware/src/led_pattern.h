/*
 * LED patterns (README "Status LED"). Pure C, no SDK: unit-tested on the host. led.c samples these every 10 ms.
 *
 *   status LED                                   meaning
 *   -------------------------------------------  -------------------------------------------------------------
 *   slow blink, 1 Hz                             powered, USB not enumerated (or suspended)
 *   short blip every 2 s                         enumerated, no host program has the port open
 *   solid on                                     host program connected (port open), idle
 *   flicker, ~12 Hz (off first)                  activity: requests / pages moving (unless a separate activity LED)
 *   fast blink, 10 Hz, for 2 s                   error reported to the host (request CRC error, R/B# timeout)
 *   triple blink, repeating forever              FAULT: firmware halted (panic), NAND bus parked; power-cycle
 *
 *   activity LED (optional, PIN_LED_ACTIVITY)    on while requests / pages are moving, like a disk LED
 */
#ifndef LED_PATTERN_H
#define LED_PATTERN_H

#include <stdbool.h>
#include <stdint.h>

#define LED_ACTIVITY_HOLD_MS 60 /* one event keeps "active" this long (pulse stretching) */
#define LED_FLICKER_HALF_MS 40  /* half-period of the activity flicker on the status LED */
#define LED_ERROR_SHOW_MS 2000
#define LED_FAULT_BLINK_MS 150  /* triple blink: 3 x (on, off) then LED_FAULT_PAUSE_MS off */
#define LED_FAULT_PAUSE_MS 1000

typedef enum {
    LED_LINK_NONE, /* USB not enumerated / suspended */
    LED_LINK_USB,  /* enumerated, port closed (DTR low) */
    LED_LINK_HOST, /* port open (DTR high) */
} led_link_t;

typedef struct {
    led_link_t link;
    bool activity_seen;
    uint32_t activity_ms;      /* time of the latest activity event */
    uint32_t flicker_start_ms; /* start of the current burst of activity */
    bool error_seen;
    uint32_t error_until_ms;
} led_state_t;

void led_pattern_note_activity(led_state_t *s, uint32_t now_ms);
void led_pattern_note_error(led_state_t *s, uint32_t now_ms);

/* Status LED level at now_ms. flicker_on_status: show activity as flicker (no separate activity LED). */
bool led_pattern_status(const led_state_t *s, uint32_t now_ms, bool flicker_on_status);

/* Separate activity LED level at now_ms. */
bool led_pattern_activity(const led_state_t *s, uint32_t now_ms);

/* FAULT pattern level, t = ms since the fault. */
bool led_pattern_fault(uint32_t t);

#endif
