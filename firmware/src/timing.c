#include "timing.h"

#include "frame.h"

_Static_assert(2 * PROTO_TIMING_FIELDS + 4 == PROTO_TIMING_WIRE_LEN, "timing_t wire length");

/* Field order == wire order == PROTO_TIMING_DEFAULT_CYCLES order.
 * Indexing the fields through this table lets the presets and the wire (de)serialisers below be simple loops, and
 * the order is written down exactly once. */
static uint16_t *timing_field(timing_t *t, unsigned i) {
    uint16_t *const f[PROTO_TIMING_FIELDS] = {
        &t->t_cs, &t->t_setup, &t->t_wp, &t->t_wh, &t->t_whr, &t->t_rea,
        &t->t_reh, &t->t_rhw, &t->t_wb, &t->t_rr, &t->t_ceh, &t->t_adl, &t->t_ww,
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

/* ns -> cycles, rounded up (a floor must never round down to less time than the datasheet asks for).
 * (a + b - 1) / b is integer ceil(a / b); the 64-bit product cannot overflow. E.g. 12 ns at 125 MHz = 1.5 -> 2. */
static uint32_t cyc(uint32_t ns, uint32_t clk_hz) {
    return (uint32_t)(((uint64_t)ns * clk_hz + 999999999u) / 1000000000u);
}

bool timing_meets_floors(const timing_t *t, uint32_t clk_hz) {
    /* Each check is the sum of our phases that spans one Table 20 constraint (PROTO_FLOOR_* in protocol_defs.h).
     * Example: tWC (write cycle time, 25 ns) is one full WE# period, which in latch_cycle() (nand_bus.c) is
     * t_setup (data valid, WE# still high) + t_wp (WE# low) + t_wh (WE# high again). So the check is their sum.
     * C promotes a uint16_t + uint16_t sum to (signed) int; the (uint32_t) casts keep the comparison with cyc()'s
     * unsigned result unsigned on both sides (no -Wsign-compare warning). */
    return (uint32_t)t->t_cs + t->t_setup + t->t_wp >= cyc(PROTO_FLOOR_TCS_NS, clk_hz)
        && t->t_cs >= cyc(PROTO_FLOOR_TCR_NS, clk_hz)
        && (uint32_t)t->t_setup + t->t_wp >= cyc(PROTO_FLOOR_TSETUP_NS, clk_hz)
        && t->t_wp >= cyc(PROTO_FLOOR_TWP_NS, clk_hz)
        && t->t_wh >= cyc(PROTO_FLOOR_THOLD_NS, clk_hz)
        && (uint32_t)t->t_wh + t->t_setup >= cyc(PROTO_FLOOR_TWH_NS, clk_hz)
        && (uint32_t)t->t_setup + t->t_wp + t->t_wh >= cyc(PROTO_FLOOR_TWC_NS, clk_hz)
        && t->t_whr >= cyc(PROTO_FLOOR_TWHR_NS, clk_hz)
        && t->t_rea >= cyc(PROTO_FLOOR_TREA_NS, clk_hz) + PROTO_FLOOR_INPUT_SYNC_CYCLES
        && t->t_reh >= cyc(PROTO_FLOOR_TREH_NS, clk_hz)
        && (uint32_t)t->t_rea + t->t_reh >= cyc(PROTO_FLOOR_TRC_NS, clk_hz)
        && t->t_rhw >= cyc(PROTO_FLOOR_TRHW_NS, clk_hz)
        && t->t_wb >= cyc(PROTO_FLOOR_TWB_NS, clk_hz)
        && t->t_rr >= cyc(PROTO_FLOOR_TRR_NS, clk_hz)
        && t->t_ceh >= cyc(PROTO_FLOOR_TCHZ_NS, clk_hz)
        && t->t_adl >= cyc(PROTO_FLOOR_TADL_NS, clk_hz)
        && t->t_ww >= cyc(PROTO_FLOOR_TWW_NS, clk_hz)
        && t->rb_timeout_us >= PROTO_FLOOR_RB_TIMEOUT_US
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
