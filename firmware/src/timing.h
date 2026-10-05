/*
 * NAND bus timing: one runtime struct, delays in clk_sys cycles. Each value is a MINIMUM; the real phase is longer by
 * instruction overhead, which is always safe because every limit the host must meet is a minimum (datasheet Table 20).
 * Presets and floors: docs/PROTOCOL.md "timing_t"; derivation: docs/PROPOSAL.md §4.2.
 * Pure C (no SDK): unit-tested on the host.
 */
#ifndef TIMING_H
#define TIMING_H

#include <stdbool.h>
#include <stdint.h>

#include "protocol_defs.h"

typedef struct {
    uint16_t t_cs;    /* CE# low -> first strobe            (tCS, tCR)            */
    uint16_t t_setup; /* CLE/ALE/IO valid -> WE# low        (tCLS, tALS, tDS)     */
    uint16_t t_wp;    /* WE# low pulse                      (tWP)                 */
    uint16_t t_wh;    /* WE# high -> change CLE/ALE/IO      (tCLH/tALH/tDH/tCH)   */
    uint16_t t_whr;   /* last WE# high -> first RE# low     (tWHR, tAR, tCLR)     */
    uint16_t t_rea;   /* RE# low -> sample IO               (tREA + input sync)   */
    uint16_t t_reh;   /* RE# high -> next RE# low           (tREH)                */
    uint16_t t_rhw;   /* last RE# high -> bus out / WE# low (tRHW, tRHZ)          */
    uint16_t t_wb;    /* last WE# high -> first R/B# sample (tWB)                 */
    uint16_t t_rr;    /* R/B# high -> first RE# low         (tRR)                 */
    uint16_t t_ceh;   /* CE# high -> next CE# low / bus out (tCSD, tCHZ)          */
    uint16_t t_adl;   /* last address WE# high -> data in   (tADL), program only  */
    uint16_t t_ww;    /* WP# high -> setup command latch    (tWW), write only     */
    uint32_t rb_timeout_us; /* R/B# poll timeout            (tR, tRST)            */
} timing_t;

_Static_assert(PROTO_TIMING_FIELDS == 13, "timing_t field count must match the wire format");

void timing_preset_default(timing_t *t);
void timing_preset_slow(timing_t *t);

/* True if every Table 20 constraint holds for t at clk_hz (sums of phases; see docs/PROTOCOL.md floors table). */
bool timing_meets_floors(const timing_t *t, uint32_t clk_hz);

void timing_to_wire(const timing_t *t, uint8_t out[PROTO_TIMING_WIRE_LEN]);
void timing_from_wire(timing_t *t, const uint8_t in[PROTO_TIMING_WIRE_LEN]);

#endif
