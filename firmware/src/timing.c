#include "timing.h"

#include "frame.h"

/* Field order == wire order == PROTO_TIMING_DEFAULT_CYCLES order. */
static uint16_t *timing_field(timing_t *t, unsigned i) {
    uint16_t *const f[PROTO_TIMING_FIELDS] = {
        &t->t_cs, &t->t_setup, &t->t_wp, &t->t_wh, &t->t_whr, &t->t_rea,
        &t->t_reh, &t->t_rhw, &t->t_wb, &t->t_rr, &t->t_ceh,
    };
    return f[i];
}

void timing_preset_default(timing_t *t) {
    static const uint16_t def[PROTO_TIMING_FIELDS] = PROTO_TIMING_DEFAULT_CYCLES;
    for (unsigned i = 0; i < PROTO_TIMING_FIELDS; i++)
        *timing_field(t, i) = def[i];
    t->rb_timeout_us = PROTO_TIMING_RB_TIMEOUT_US;
}

void timing_preset_slow(timing_t *t) {
    for (unsigned i = 0; i < PROTO_TIMING_FIELDS; i++)
        *timing_field(t, i) = PROTO_TIMING_SLOW_CYCLES;
    t->rb_timeout_us = PROTO_TIMING_RB_TIMEOUT_US;
}

/* ns -> cycles, rounded up. */
static uint32_t cyc(uint32_t ns, uint32_t clk_hz) {
    return (uint32_t)(((uint64_t)ns * clk_hz + 999999999u) / 1000000000u);
}

#define INPUT_SYNC_CYCLES 2 /* RP2040 GPIO input synchronizer */

bool timing_meets_floors(const timing_t *t, uint32_t clk_hz) {
    /* Datasheet Table 20 (ns); each check is the sum of our phases that spans the constraint. */
    return (uint32_t)t->t_cs + t->t_setup + t->t_wp >= cyc(20, clk_hz)    /* tCS  (CE# low -> WE# high) */
        && t->t_cs >= cyc(10, clk_hz)                                     /* tCR                        */
        && (uint32_t)t->t_setup + t->t_wp >= cyc(10, clk_hz)              /* tCLS, tALS, tDS            */
        && t->t_wp >= cyc(12, clk_hz)                                     /* tWP                        */
        && t->t_wh >= cyc(5, clk_hz)                                      /* tCLH, tALH, tDH, tCH       */
        && (uint32_t)t->t_wh + t->t_setup >= cyc(10, clk_hz)              /* tWH                        */
        && (uint32_t)t->t_setup + t->t_wp + t->t_wh >= cyc(25, clk_hz)    /* tWC                        */
        && t->t_whr >= cyc(60, clk_hz)                                    /* tWHR (>= tAR, tCLR)        */
        && t->t_rea >= cyc(20, clk_hz) + INPUT_SYNC_CYCLES                /* tREA max + sync (>= tRP)   */
        && t->t_reh >= cyc(10, clk_hz)                                    /* tREH                       */
        && (uint32_t)t->t_rea + t->t_reh >= cyc(25, clk_hz)               /* tRC                        */
        && t->t_rhw >= cyc(100, clk_hz)                                   /* tRHW, tRHZ                 */
        && t->t_wb >= cyc(100, clk_hz)                                    /* tWB max                    */
        && t->t_rr >= cyc(20, clk_hz)                                     /* tRR                        */
        && t->t_ceh >= cyc(30, clk_hz)                                    /* tCHZ max (>= tCSD)         */
        && t->rb_timeout_us >= 25                                         /* tR                         */
        && t->rb_timeout_us <= PROTO_TIMING_RB_TIMEOUT_MAX_US;
}

void timing_to_wire(const timing_t *t, uint8_t out[PROTO_TIMING_WIRE_LEN]) {
    for (unsigned i = 0; i < PROTO_TIMING_FIELDS; i++)
        put_le16(&out[2 * i], *timing_field((timing_t *)t, i));
    put_le32(&out[2 * PROTO_TIMING_FIELDS], t->rb_timeout_us);
}

void timing_from_wire(timing_t *t, const uint8_t in[PROTO_TIMING_WIRE_LEN]) {
    for (unsigned i = 0; i < PROTO_TIMING_FIELDS; i++)
        *timing_field(t, i) = get_le16(&in[2 * i]);
    t->rb_timeout_us = get_le32(&in[2 * PROTO_TIMING_FIELDS]);
}
