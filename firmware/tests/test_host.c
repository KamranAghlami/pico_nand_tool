/* Host-compiled unit tests for the SDK-free firmware modules: crc32, frame, timing. Run: make -C firmware/tests check */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "crc32.h"
#include "frame.h"
#include "led_pattern.h"
#include "nand_cmd.h"
#include "timing.h"

static int failures;
#define CHECK(cond)                                                          \
    do {                                                                     \
        if (!(cond)) {                                                       \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
            failures++;                                                      \
        }                                                                    \
    } while (0)

#define CLK 125000000u

static void test_crc32(void) {
    CHECK(crc32("123456789", 9) == 0xCBF43926u); /* standard check value (PROTOCOL.md) */
    CHECK(crc32("", 0) == 0);
    uint32_t c = crc32_update(CRC32_INIT, "1234", 4);
    c = crc32_update(c, "56789", 5);
    CHECK(crc32_final(c) == 0xCBF43926u);
}

/* Feed bytes; return number of OK frames, last one in *out; count bad CRCs. */
static int feed_all(frame_parser_t *p, const uint8_t *b, unsigned n, proto_req_t *out, int *bad_crc) {
    int ok = 0;
    for (unsigned i = 0; i < n; i++) {
        proto_req_t r;
        frame_result_t res = frame_parser_feed(p, b[i], &r);
        if (res == FRAME_OK) {
            *out = r;
            ok++;
        } else if (res == FRAME_BAD_CRC) {
            *out = r;
            (*bad_crc)++;
        }
    }
    return ok;
}

static void test_frame_roundtrip(void) {
    uint8_t f[FRAME_REQ_MAX];
    const uint8_t args[] = {0x10, 0x20, 0x30};
    unsigned n = frame_req_encode(f, PROTO_CMD_READ_PAGES, 7, args, 3);
    CHECK(n == 4 + 3 + 4);
    CHECK(f[0] == 0xA5 && f[1] == 0x08 && f[2] == 7 && f[3] == 3);
    CHECK(get_le32(&f[7]) == crc32(f, 7));

    frame_parser_t p;
    frame_parser_reset(&p);
    proto_req_t r;
    int bad = 0;
    CHECK(feed_all(&p, f, n, &r, &bad) == 1 && bad == 0);
    CHECK(r.cmd == 0x08 && r.seq == 7 && r.arg_len == 3 && memcmp(r.args, args, 3) == 0);
    CHECK(frame_parser_idle(&p));

    /* Zero-arg and maximum-arg frames */
    uint8_t big[PROTO_MAX_ARGS];
    for (unsigned i = 0; i < sizeof big; i++)
        big[i] = (uint8_t)(0xA5 ^ i);
    n = frame_req_encode(f, PROTO_CMD_SET_TIMING, 1, big, PROTO_MAX_ARGS);
    CHECK(n == FRAME_REQ_MAX);
    CHECK(feed_all(&p, f, n, &r, &bad) == 1 && r.arg_len == PROTO_MAX_ARGS && memcmp(r.args, big, sizeof big) == 0);
    n = frame_req_encode(f, PROTO_CMD_PING, 2, NULL, 0);
    CHECK(feed_all(&p, f, n, &r, &bad) == 1 && r.cmd == PROTO_CMD_PING && r.arg_len == 0);
}

static void test_frame_bad_crc(void) {
    uint8_t f[FRAME_REQ_MAX];
    unsigned n = frame_req_encode(f, PROTO_CMD_PING, 9, NULL, 0);
    f[n - 1] ^= 0x01;
    frame_parser_t p;
    frame_parser_reset(&p);
    proto_req_t r;
    int bad = 0;
    CHECK(feed_all(&p, f, n, &r, &bad) == 0 && bad == 1);
    CHECK(r.cmd == PROTO_CMD_PING && r.seq == 9); /* echoed as received */
    /* Parser recovers for the next frame */
    n = frame_req_encode(f, PROTO_CMD_PING, 10, NULL, 0);
    CHECK(feed_all(&p, f, n, &r, &bad) == 1 && r.seq == 10);
}

static void test_frame_resync(void) {
    uint8_t f[FRAME_REQ_MAX];
    unsigned n = frame_req_encode(f, PROTO_CMD_PING, 3, NULL, 0);
    frame_parser_t p;
    proto_req_t r;
    int bad = 0;

    /* Leading garbage (including the response magic) is skipped. */
    uint8_t s1[64] = {0x00, 0x5A, 0x13};
    memcpy(&s1[3], f, n);
    frame_parser_reset(&p);
    CHECK(feed_all(&p, s1, 3 + n, &r, &bad) == 1 && bad == 0 && r.seq == 3);

    /* False magic: A5 00 00 FF has arg_len > 32, so scanning resumes and the following frame parses cleanly. */
    uint8_t s2[64] = {0xA5, 0x00, 0x00, 0xFF};
    memcpy(&s2[4], f, n);
    frame_parser_reset(&p);
    CHECK(feed_all(&p, s2, 4 + n, &r, &bad) == 1 && bad == 0 && r.seq == 3);

    /* A false magic inside the rescanned bytes: A5 A5 01 FF -> rescan "A5 01 FF" -> rescan "01 FF" -> idle. */
    uint8_t s3[64] = {0xA5, 0xA5, 0x01, 0xFF};
    memcpy(&s3[4], f, n);
    frame_parser_reset(&p);
    CHECK(feed_all(&p, s3, 4 + n, &r, &bad) == 1 && bad == 0 && r.seq == 3);
}

static void test_resp_header(void) {
    uint8_t h[PROTO_RESP_HDR_LEN];
    frame_resp_header(h, 0x88, 0x42, PROTO_ST_ERR_ABORTED, 0x01020304u, 2112);
    const uint8_t want[] = {0x5A, 0x88, 0x42, 0x05, 0x04, 0x03, 0x02, 0x01, 0x40, 0x08};
    CHECK(memcmp(h, want, sizeof want) == 0);
}

static void test_timing(void) {
    timing_t t;
    timing_preset_default(&t);
    CHECK(timing_meets_floors(&t, CLK));
    CHECK(t.t_cs == 6 && t.t_rea == 8 && t.t_ceh == 8 && t.rb_timeout_us == 1000);
    timing_preset_slow(&t);
    CHECK(timing_meets_floors(&t, CLK));
    CHECK(t.t_wp == 125 && t.t_rr == 125);

    timing_preset_default(&t);
    t.t_wp = 1; /* 8 ns < tWP 12 */
    CHECK(!timing_meets_floors(&t, CLK));
    timing_preset_default(&t);
    t.t_rea = 4; /* 32 ns < tREA 20 + 2-cycle sync = 5 cycles */
    CHECK(!timing_meets_floors(&t, CLK));
    t.t_rea = 5;
    CHECK(timing_meets_floors(&t, CLK));
    timing_preset_default(&t);
    t.t_whr = 7; /* 56 ns < tWHR 60 */
    CHECK(!timing_meets_floors(&t, CLK));
    timing_preset_default(&t);
    t.rb_timeout_us = 0;
    CHECK(!timing_meets_floors(&t, CLK));
    t.rb_timeout_us = PROTO_TIMING_RB_TIMEOUT_MAX_US + 1;
    CHECK(!timing_meets_floors(&t, CLK));
    /* A faster clock needs more cycles: defaults at 250 MHz would put t_whr (15 cyc = 60 ns) exactly on the floor */
    timing_preset_default(&t);
    CHECK(timing_meets_floors(&t, 250000000u));
    t.t_whr = 14;
    CHECK(!timing_meets_floors(&t, 250000000u));

    uint8_t w[PROTO_TIMING_WIRE_LEN];
    timing_preset_default(&t);
    t.rb_timeout_us = 0x01020304u;
    timing_to_wire(&t, w);
    CHECK(w[0] == 6 && w[1] == 0 && w[20] == 8 && w[22] == 0x04 && w[25] == 0x01);
    timing_t u;
    timing_from_wire(&u, w);
    uint8_t w2[PROTO_TIMING_WIRE_LEN];
    timing_to_wire(&u, w2);
    CHECK(memcmp(w, w2, sizeof w) == 0 && u.rb_timeout_us == 0x01020304u && u.t_ceh == 8);
}

static void test_opcode_allow_list(void) {
    /* Runtime layer of the gate: exactly the six SPEC opcodes, and they match the enum. */
    const unsigned allowed[] = {NAND_CMD_READ_1, NAND_CMD_READ_2, NAND_CMD_READ_STATUS,
                                NAND_CMD_READ_ID, NAND_CMD_READ_PARAM, NAND_CMD_RESET};
    const unsigned want[] = {0x00, 0x30, 0x70, 0x90, 0xEC, 0xFF};
    unsigned n = 0;
    for (unsigned op = 0; op < 256; op++)
        n += nand_cmd_is_allowed(op) ? 1 : 0;
    CHECK(n == 6);
    for (unsigned i = 0; i < 6; i++)
        CHECK(allowed[i] == want[i] && nand_cmd_is_allowed(want[i]));
    CHECK(!nand_cmd_is_allowed(0x80) && !nand_cmd_is_allowed(0x10) && !nand_cmd_is_allowed(0x60) &&
          !nand_cmd_is_allowed(0xD0) && !nand_cmd_is_allowed(0x85) && !nand_cmd_is_allowed(0x100));
}

/* Count level changes of f over [t0, t1) sampled every ms. */
static int edges(const led_state_t *s, uint32_t t0, uint32_t t1, bool flicker) {
    int n = 0;
    bool prev = led_pattern_status(s, t0, flicker);
    for (uint32_t t = t0 + 1; t < t1; t++) {
        bool v = led_pattern_status(s, t, flicker);
        n += v != prev;
        prev = v;
    }
    return n;
}

static void test_led_patterns(void) {
    led_state_t s = {0};

    s.link = LED_LINK_NONE; /* 1 Hz: 2 edges per second */
    CHECK(edges(&s, 0, 1000, true) == 1 && edges(&s, 0, 4000, true) == 7);
    CHECK(led_pattern_status(&s, 100, true) && !led_pattern_status(&s, 600, true));

    s.link = LED_LINK_USB; /* heartbeat: short blip every 2 s, mostly off */
    CHECK(led_pattern_status(&s, 10, true) && !led_pattern_status(&s, 100, true) && !led_pattern_status(&s, 1500, true));
    CHECK(led_pattern_status(&s, 2010, true));

    s.link = LED_LINK_HOST; /* idle: solid */
    CHECK(edges(&s, 0, 5000, true) == 0 && led_pattern_status(&s, 1234, true));

    /* one activity event: an immediate off blip, then back to solid */
    led_pattern_note_activity(&s, 10000);
    CHECK(!led_pattern_status(&s, 10000, true) && !led_pattern_status(&s, 10039, true));
    CHECK(led_pattern_status(&s, 10040, true));
    CHECK(led_pattern_status(&s, 10000 + LED_ACTIVITY_HOLD_MS + 1, true));
    CHECK(led_pattern_activity(&s, 10000) && led_pattern_activity(&s, 10059) && !led_pattern_activity(&s, 10060));
    /* with a separate activity LED, the status LED stays solid */
    CHECK(led_pattern_status(&s, 10005, false));

    /* continuous activity (an event every 5 ms for 1 s): steady ~12.5 Hz flicker, activity LED solid on */
    for (uint32_t t = 20000; t < 21000; t += 5)
        led_pattern_note_activity(&s, t);
    CHECK(edges(&s, 20000, 21000, true) >= 23 && edges(&s, 20000, 21000, true) <= 25);
    for (uint32_t t = 20000; t < 21000; t++)
        CHECK(led_pattern_activity(&s, t) || t > 20995);

    /* error: 10 Hz for 2 s overrides everything, then back to solid */
    led_pattern_note_error(&s, 30000);
    CHECK(edges(&s, 30000, 32000, true) == 39);
    CHECK(led_pattern_status(&s, 32000, true) && edges(&s, 32000, 35000, true) == 0);

    /* timestamps wrap after ~49 days */
    led_state_t w = {.link = LED_LINK_HOST};
    led_pattern_note_activity(&w, 0xFFFFFFF0u);
    CHECK(led_pattern_activity(&w, 0x00000010u) && !led_pattern_activity(&w, 0x00000100u));

    /* fault: 3 blinks then a pause, repeating */
    int on_ms = 0, n = 0;
    bool prev = false;
    for (uint32_t t = 0; t < 6 * LED_FAULT_BLINK_MS + LED_FAULT_PAUSE_MS; t++) {
        bool v = led_pattern_fault(t);
        on_ms += v;
        n += v && !prev;
        prev = v;
    }
    CHECK(n == 3 && on_ms == 3 * LED_FAULT_BLINK_MS);
    CHECK(!led_pattern_fault(6 * LED_FAULT_BLINK_MS + 10) && led_pattern_fault(6 * LED_FAULT_BLINK_MS + LED_FAULT_PAUSE_MS));
}

int main(void) {
    test_opcode_allow_list();
    test_led_patterns();
    test_crc32();
    test_frame_roundtrip();
    test_frame_bad_crc();
    test_frame_resync();
    test_resp_header();
    test_timing();
    if (failures) {
        fprintf(stderr, "%d check(s) FAILED\n", failures);
        return EXIT_FAILURE;
    }
    puts("firmware host tests: all passed");
    return EXIT_SUCCESS;
}
