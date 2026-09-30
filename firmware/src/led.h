/* Status LED and optional disk-activity LED (patterns: led_pattern.h). Safe to call from any context. */
#ifndef LED_H
#define LED_H

#include "led_pattern.h"

/* Configure the LED pin(s) and start the 10 ms refresh timer (so patterns keep running while the CPU is busy). */
void led_init(void);

void led_set_link(led_link_t link);
void led_activity(void); /* a request or page moved */
void led_error(void);    /* an error was reported to the host */

/* FAULT pattern forever. For the panic handler only: interrupts must already be disabled. */
void __attribute__((noreturn)) led_fault_forever(void);

#endif
