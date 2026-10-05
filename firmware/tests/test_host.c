/* Host-compiled unit tests for the SDK-free firmware modules: crc32, frame, timing. Run: make -C firmware/tests check */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "crc32.h"
#include "frame.h"
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
    CHECK(n == 5 + 3 + 4);
    CHECK(f[0] == 0xA5 && f[1] == 0x08 && f[2] == 7 && f[3] == 3 && f[4] == 0); /* u16 arg_len (v2) */
    CHECK(get_le32(&f[8]) == crc32(f, 8));

    frame_parser_t p;
    frame_parser_reset(&p);
    static proto_req_t r;
    int bad = 0;
    CHECK(feed_all(&p, f, n, &r, &bad) == 1 && bad == 0);
    CHECK(r.cmd == 0x08 && r.seq == 7 && r.arg_len == 3 && memcmp(r.args, args, 3) == 0);
    CHECK(frame_parser_idle(&p));

    /* Zero-arg and maximum-arg frames (PROGRAM_PAGE: u32 page + 2112 bytes) */
    static uint8_t big[PROTO_MAX_ARGS];
    for (unsigned i = 0; i < sizeof big; i++)
        big[i] = (uint8_t)(0xA5 ^ i);
    n = frame_req_encode(f, PROTO_CMD_PROGRAM_PAGE, 1, big, PROTO_MAX_ARGS);
    CHECK(n == FRAME_REQ_MAX && PROTO_MAX_ARGS == 4 + PROTO_PAGE_LEN);
    CHECK(f[3] == (PROTO_MAX_ARGS & 0xFF) && f[4] == (PROTO_MAX_ARGS >> 8));
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
    static proto_req_t r;
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
    static proto_req_t r;
    int bad = 0;

    /* Leading garbage (including the response magic) is skipped. */
    uint8_t s1[64] = {0x00, 0x5A, 0x13};
    memcpy(&s1[3], f, n);
    frame_parser_reset(&p);
    CHECK(feed_all(&p, s1, 3 + n, &r, &bad) == 1 && bad == 0 && r.seq == 3);

    /* False magic: A5 00 00 45 08 has arg_len 2117 > 2116, so scanning resumes and the following frame parses. */
    uint8_t s2[64] = {0xA5, 0x00, 0x00, 0x45, 0x08};
    memcpy(&s2[5], f, n);
    frame_parser_reset(&p);
    CHECK(feed_all(&p, s2, 5 + n, &r, &bad) == 1 && bad == 0 && r.seq == 3);

    /* A false magic inside the rescanned bytes: A5 A5 01 FF FF -> rescan "A5 01 FF FF" + the frame's A5 (arg_len
     * A5FFh) -> rescan "01 FF FF A5" -> the frame's magic starts a new header. */
    uint8_t s3[64] = {0xA5, 0xA5, 0x01, 0xFF, 0xFF};
    memcpy(&s3[5], f, n);
    frame_parser_reset(&p);
    CHECK(feed_all(&p, s3, 5 + n, &r, &bad) == 1 && bad == 0 && r.seq == 3);
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
    t.t_adl = 8; /* 64 ns < tADL 70 */
    CHECK(!timing_meets_floors(&t, CLK));
    t.t_adl = 9;
    CHECK(timing_meets_floors(&t, CLK));
    t.t_ww = 12; /* 96 ns < tWW 100 */
    CHECK(!timing_meets_floors(&t, CLK));
    t.t_ww = 13;
    CHECK(timing_meets_floors(&t, CLK));
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
    CHECK(w[0] == 6 && w[1] == 0 && w[20] == 8 && w[22] == 18 && w[24] == 25 && w[26] == 0x04 && w[29] == 0x01);
    timing_t u;
    timing_from_wire(&u, w);
    uint8_t w2[PROTO_TIMING_WIRE_LEN];
    timing_to_wire(&u, w2);
    CHECK(memcmp(w, w2, sizeof w) == 0 && u.rb_timeout_us == 0x01020304u && u.t_ceh == 8 && u.t_adl == 18 &&
          u.t_ww == 25);
}

static void test_opcode_allow_list(void) {
    /* Runtime layer of the gate: exactly the six read and four write opcodes of the SPEC, matching the enum. */
    const unsigned rd[] = {NAND_CMD_READ_1, NAND_CMD_READ_2, NAND_CMD_READ_STATUS,
                           NAND_CMD_READ_ID, NAND_CMD_READ_PARAM, NAND_CMD_RESET};
    const unsigned rd_want[] = {0x00, 0x30, 0x70, 0x90, 0xEC, 0xFF};
    const unsigned wr[] = {NAND_CMD_PROGRAM_1, NAND_CMD_PROGRAM_2, NAND_CMD_ERASE_1, NAND_CMD_ERASE_2};
    const unsigned wr_want[] = {0x80, 0x10, 0x60, 0xD0};
    unsigned n = 0, nr = 0, nw = 0;
    for (unsigned op = 0; op < 256; op++) {
        n += nand_cmd_is_allowed(op) ? 1 : 0;
        nr += nand_cmd_is_read(op) ? 1 : 0;
        nw += nand_cmd_is_write(op) ? 1 : 0;
        CHECK(!(nand_cmd_is_read(op) && nand_cmd_is_write(op)));
    }
    CHECK(n == 10 && nr == 6 && nw == 4);
    for (unsigned i = 0; i < 6; i++)
        CHECK(rd[i] == rd_want[i] && nand_cmd_is_read(rd_want[i]));
    for (unsigned i = 0; i < 4; i++)
        CHECK(wr[i] == wr_want[i] && nand_cmd_is_write(wr_want[i]));
    /* Random data input, copy-back, cache, multiplane, Read Status Enhanced, OTP-style opcodes stay out. */
    const unsigned never[] = {0x85, 0x35, 0x11, 0x81, 0x15, 0x31, 0x3F, 0x78, 0x8B, 0x29, 0xA0, 0x100};
    for (unsigned i = 0; i < sizeof never / sizeof never[0]; i++)
        CHECK(!nand_cmd_is_allowed(never[i]));
}

int main(void) {
    test_opcode_allow_list();
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
