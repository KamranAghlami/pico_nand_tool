/* Request dispatch and response framing over the raw CDC channel (docs/PROTOCOL.md). */
#ifndef PROTOCOL_H
#define PROTOCOL_H

#include "timing.h"

void protocol_init(void);

/* Call from the main loop after tud_task(): reads pending bytes and executes complete requests. */
void protocol_poll(void);

/* The active bus timing (set by SET_TIMING). */
const timing_t *protocol_timing(void);

#endif
