/*
 * Request dispatch, response framing and the USB side of the protocol (docs/PROTOCOL.md).
 *
 * Study notes
 *  - Flow: poll_input() reads USB bytes -> frame_parser_feed() -> a complete, CRC-valid request -> dispatch() ->
 *    cmd_xxx() -> NAND work (nand_ops.c / nand_write.c) -> send_resp(). One request is handled completely before the
 *    next byte is parsed, so responses go out in request order.
 *  - Every cmd_xxx() validates arg_len and the argument values first and answers ERR_BAD_ARGS before doing anything.
 *  - Big buffers are `static` (file-lifetime, not on the stack): the SDK gives the main stack only 2 KB, and one
 *    proto_req_t alone is 2 KB.
 *  - "Session": the host opening or closing the serial port toggles DTR. Each change bumps `session`, so code that
 *    is busy sending can tell that the host it was talking to has gone, and stop (see usb_write_all()).
 *  - Re-entrancy: during a READ_PAGES stream, cmd_read_pages() calls poll_input() itself, between pages, so it can
 *    see an ABORT. That nested call uses a different handler (stream_on_req) and request buffer, but shares the
 *    receive buffer and the parser, so bytes are still consumed strictly in arrival order.
 */
#include "protocol.h"

#include <string.h>

#include "crc32.h"
#include "frame.h"
#include "hardware/clocks.h"
#include "hardware/watchdog.h"
#include "nand_ops.h"
#include "nand_write.h"
#include "pico/bootrom.h"
#include "pico/time.h"
#include "tusb.h"
#include "version.h"

static frame_parser_t parser;
static uint32_t last_rx_ms;
static timing_t timing;
static uint8_t timing_mode;
/* bumped on every DTR change (tud_cdc_line_state_cb). volatile marks it as changed outside the normal flow (by a
 * callback that runs from inside tud_task(), which the send loops call), so every check reads it fresh. */
static volatile uint32_t session;

void protocol_init(void) {
    frame_parser_reset(&parser);
    timing_preset_default(&timing);
    timing_mode = PROTO_TIMING_DEFAULT;
}

const timing_t *protocol_timing(void) { return &timing; }

/* ---- output ---------------------------------------------------------------------------------------------- */

/* Write everything, running tud_task() while the TX FIFO is full: the device never drops bytes and never overruns
 * (PROTOCOL "Transport"). Gives up if the host goes away or reconnects (DTR change), so it can't block forever and
 * never finishes an old session's frame into a new session. */
static bool usb_write_all(const uint8_t *p, uint32_t n, uint32_t my_session) {
    while (n) {
        if (!tud_cdc_connected() || session != my_session)
            return false;
        uint32_t avail = tud_cdc_write_available();
        if (avail == 0) {
            tud_cdc_write_flush();
            tud_task();
            watchdog_update(); /* waiting for the host to read is not a hang */
            continue;
        }
        uint32_t k = tud_cdc_write(p, n < avail ? n : avail);
        p += k;
        n -= k;
    }
    return true;
}

/* One response frame: header, payload, CRC. The CRC is computed incrementally over header + payload (crc32_update),
 * so the payload never has to be copied next to the header into one buffer. */
static bool send_resp(uint8_t cmd, uint8_t seq, uint8_t status, uint32_t page, const uint8_t *payload,
                      uint16_t len) {
    uint8_t hdr[PROTO_RESP_HDR_LEN];
    uint8_t tail[PROTO_CRC_LEN];
    frame_resp_header(hdr, cmd, seq, status, page, len);
    uint32_t c = crc32_update(CRC32_INIT, hdr, sizeof hdr);
    c = crc32_update(c, payload, len);
    put_le32(tail, crc32_final(c));

    uint32_t s = session; /* one session for the whole frame */
    bool ok = usb_write_all(hdr, sizeof hdr, s) && usb_write_all(payload, len, s) && usb_write_all(tail, sizeof tail, s);
    tud_cdc_write_flush();
    return ok;
}

static void send_status(const proto_req_t *r, uint8_t status) {
    send_resp(r->cmd, r->seq, status, PROTO_PAGE_NONE, NULL, 0);
}

/* ---- commands -------------------------------------------------------------------------------------------- */

static uint32_t cycles_to_ns(uint32_t cycles) {
    return (uint32_t)((uint64_t)cycles * 1000000000u / clock_get_hz(clk_sys));
}

static void cmd_ping(const proto_req_t *r) {
    if (r->arg_len != 0) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }

    static const char ver[] = FW_VERSION_STRING;
    uint8_t buf[8 + sizeof ver - 1];
    buf[0] = PROTO_VERSION;
    buf[1] = FW_VERSION_MAJOR;
    buf[2] = FW_VERSION_MINOR;
    buf[3] = FW_VERSION_PATCH;
    put_le32(&buf[4], clock_get_hz(clk_sys));
    memcpy(&buf[8], ver, sizeof ver - 1); /* no NUL on the wire */
    send_resp(r->cmd, r->seq, PROTO_ST_OK, PROTO_PAGE_NONE, buf, sizeof buf);
}

static void cmd_set_timing(const proto_req_t *r) {
    uint8_t mode = r->arg_len ? r->args[0] : 0xFF;

    if (r->arg_len == 1 && mode == PROTO_TIMING_DEFAULT) {
        timing_preset_default(&timing);
        timing_mode = mode;
    } else if (r->arg_len == 1 && mode == PROTO_TIMING_SLOW) {
        timing_preset_slow(&timing);
        timing_mode = mode;
    } else if (r->arg_len == 1 + PROTO_TIMING_WIRE_LEN && mode == PROTO_TIMING_CUSTOM) {
        timing_t t;
        timing_from_wire(&t, &r->args[1]);
        if (!timing_meets_floors(&t, clock_get_hz(clk_sys))) {
            send_status(r, PROTO_ST_ERR_TIMING_FLOOR); /* rejected, not clamped; nothing changed */
            return;
        }
        timing = t;
        timing_mode = mode;
    } else if (!(r->arg_len == 1 && mode == PROTO_TIMING_QUERY)) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }

    uint8_t buf[1 + PROTO_TIMING_WIRE_LEN];
    buf[0] = timing_mode;
    timing_to_wire(&timing, &buf[1]);
    send_resp(r->cmd, r->seq, PROTO_ST_OK, PROTO_PAGE_NONE, buf, sizeof buf);
}

static void cmd_reset(const proto_req_t *r) {
    if (r->arg_len != 0) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }
    nand_write_disarm(); /* PROTOCOL "Write mode": RESET disarms */
    uint32_t busy_cycles = 0;
    uint8_t st = nand_reset(&busy_cycles);
    if (st != PROTO_ST_OK) {
        send_status(r, st);
        return;
    }
    uint8_t buf[4];
    put_le32(buf, cycles_to_ns(busy_cycles));
    send_resp(r->cmd, r->seq, PROTO_ST_OK, PROTO_PAGE_NONE, buf, sizeof buf);
}

static void cmd_read_id(const proto_req_t *r) {
    uint8_t addr = 0x00, n = 5; /* no args: manufacturer + device ID (§3.16) */
    if (r->arg_len == 2) {
        addr = r->args[0];
        n = r->args[1];
    }
    /* 00h = ID (§3.16), 20h = ONFI signature (§3.18). Nothing else (PROPOSAL §1 item 8). */
    if ((r->arg_len != 0 && r->arg_len != 2) || (addr != 0x00 && addr != 0x20) || n < 1 || n > 8) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }
    uint8_t buf[8];
    uint8_t st = nand_read_id(addr, buf, n);
    send_resp(r->cmd, r->seq, st, PROTO_PAGE_NONE, buf, st == PROTO_ST_OK ? n : 0);
}

static void cmd_read_status(const proto_req_t *r) {
    if (r->arg_len != 0) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }
    uint8_t sr;
    uint8_t st = nand_read_status(&sr);
    send_resp(r->cmd, r->seq, st, PROTO_PAGE_NONE, &sr, st == PROTO_ST_OK ? 1 : 0);
}

static void cmd_read_param(const proto_req_t *r) {
    if (r->arg_len != 0) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }
    static uint8_t buf[PROTO_PARAM_LEN]; /* static: keeps 768 B off the 2 KB main stack */
    uint8_t st = nand_read_param(buf, sizeof buf);
    send_resp(r->cmd, r->seq, st, PROTO_PAGE_NONE, buf, st == PROTO_ST_OK ? sizeof buf : 0);
}

/* ---- write mode (docs/PROTOCOL.md "Write mode") ------------------------------------------------------------ */

static void cmd_arm_write(const proto_req_t *r) {
    if (r->arg_len != PROTO_ARM_LEN) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }
    send_status(r, nand_write_arm(get_le32(&r->args[0]), get_le16(&r->args[4]), get_le16(&r->args[6]),
                                  get_le16(&r->args[8])));
}

static void cmd_disarm(const proto_req_t *r) {
    if (r->arg_len != 0) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }
    nand_write_disarm();
    send_status(r, PROTO_ST_OK);
}

/* OK, ERR_OP_FAILED and ERR_WP_STUCK carry u8 sr + u32 busy_ns; every other status has no payload. */
static void send_write_result(const proto_req_t *r, uint32_t page, uint8_t st, uint8_t sr, uint32_t busy_cycles) {
    uint8_t buf[PROTO_WRITE_RESP_LEN];
    bool with_sr = st == PROTO_ST_OK || st == PROTO_ST_ERR_OP_FAILED || st == PROTO_ST_ERR_WP_STUCK;
    buf[0] = sr;
    put_le32(&buf[1], cycles_to_ns(busy_cycles));
    send_resp(r->cmd, r->seq, st, page, buf, with_sr ? sizeof buf : 0);
}

static void cmd_erase_block(const proto_req_t *r) {
    uint16_t block = r->arg_len == PROTO_ERASE_LEN ? get_le16(&r->args[0]) : 0;
    uint8_t flags = r->arg_len == PROTO_ERASE_LEN ? r->args[2] : 0;
    if (r->arg_len != PROTO_ERASE_LEN || block >= PROTO_BLOCKS || (flags & ~PROTO_ERASE_IGNORE_BAD_MARKER)) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }
    uint8_t sr = 0;
    uint32_t busy = 0;
    uint8_t st = nand_erase_block(block, flags & PROTO_ERASE_IGNORE_BAD_MARKER, &sr, &busy);
    send_write_result(r, (uint32_t)block * PROTO_PAGES_PER_BLOCK, st, sr, busy);
}

static void cmd_program_page(const proto_req_t *r) {
    uint32_t page = r->arg_len == 4 + PROTO_PAGE_LEN ? get_le32(&r->args[0]) : 0;
    if (r->arg_len != 4 + PROTO_PAGE_LEN || page >= PROTO_TOTAL_PAGES) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }
    uint8_t sr = 0;
    uint32_t busy = 0;
    uint8_t st = nand_program_page(page, &r->args[4], &sr, &busy);
    send_write_result(r, page, st, sr, busy);
}

/* ---- READ_PAGES stream (docs/PROTOCOL.md "READ_PAGES stream") --------------------------------------------- */

static void poll_input(void (*on_req)(const proto_req_t *r), proto_req_t *req);

static bool stream_abort;
static uint8_t stream_abort_seq;

/* Requests that arrive while a stream runs. The first valid ABORT ends the stream after the current page (its OK
 * follows the end frame); everything else gets an immediate ERR_BUSY with its own seq, between page frames. */
static void stream_on_req(const proto_req_t *r) {
    if (r->cmd == PROTO_CMD_ABORT && r->arg_len == 0 && !stream_abort) {
        stream_abort = true;
        stream_abort_seq = r->seq;
        return;
    }
    send_status(r, PROTO_ST_ERR_BUSY);
}

/* The stream: one page frame per page, then an end frame (cmd | 0x80) with pages_sent / pages_failed.
 * Between pages: service USB, feed the watchdog, look for an ABORT, check the host is still there. A page whose
 * R/B# wait timed out is reported (status ERR_RB_TIMEOUT, no data) rather than skipped or filled in, and the stream
 * carries on; the host decides whether to re-read it. */
static void cmd_read_pages(const proto_req_t *r) {
    uint32_t start = r->arg_len == 8 ? get_le32(&r->args[0]) : 0;
    uint32_t count = r->arg_len == 8 ? get_le32(&r->args[4]) : 0;
    if (count == 0 || start >= PROTO_TOTAL_PAGES || count > PROTO_TOTAL_PAGES - start) {
        send_status(r, PROTO_ST_ERR_BAD_ARGS);
        return;
    }

    static uint8_t page_buf[PROTO_PAGE_LEN]; /* static: off the 2 KB main stack */
    uint32_t my_session = session;
    uint32_t sent = 0, failed = 0;
    stream_abort = false;

    while (sent < count) {
        static proto_req_t stream_req; /* static: 2 KB. Not the top-level buffer, which still holds *r */
        tud_task();
        watchdog_update();
        poll_input(stream_on_req, &stream_req);
        if (session != my_session || !tud_cdc_connected())
            return; /* DTR dropped: the host is gone, nobody to send the end frame to */
        if (stream_abort)
            break;

        uint32_t p = start + sent;
        uint8_t st = nand_read_page(p, page_buf, sizeof page_buf);
        bool ok;
        if (st == PROTO_ST_OK) {
            ok = send_resp(r->cmd, r->seq, PROTO_ST_OK, p, page_buf, sizeof page_buf);
        } else {
            failed++;
            (void)nand_reset(NULL); /* FFh to recover, then continue with p + 1; the host re-requests p */
            ok = send_resp(r->cmd, r->seq, st, p, NULL, 0);
        }
        sent++;
        if (!ok)
            return; /* host went away mid-frame */
    }

    uint8_t end[PROTO_END_LEN];
    put_le32(&end[0], sent);
    put_le32(&end[4], failed);
    send_resp(r->cmd | PROTO_END_FLAG, r->seq, stream_abort ? PROTO_ST_ERR_ABORTED : PROTO_ST_OK, start + sent, end,
              sizeof end);
    if (stream_abort)
        send_resp(PROTO_CMD_ABORT, stream_abort_seq, PROTO_ST_OK, PROTO_PAGE_NONE, NULL, 0);
}

static void cmd_abort(const proto_req_t *r) {
    /* No stream is running here (a running stream handles ABORT itself), so ABORT is a no-op. */
    send_status(r, r->arg_len ? PROTO_ST_ERR_BAD_ARGS : PROTO_ST_OK);
}

/* One handler per command code (protocol_defs.h PROTO_CMD_*). Each handler sends exactly one response, except
 * READ_PAGES, which streams. */
static void dispatch(const proto_req_t *r) {
    switch (r->cmd) {
    case PROTO_CMD_PING:
        cmd_ping(r);
        break;
    case PROTO_CMD_SET_TIMING:
        cmd_set_timing(r);
        break;
    case PROTO_CMD_RESET:
        cmd_reset(r);
        break;
    case PROTO_CMD_READ_ID:
        cmd_read_id(r);
        break;
    case PROTO_CMD_READ_STATUS:
        cmd_read_status(r);
        break;
    case PROTO_CMD_READ_PARAM:
        cmd_read_param(r);
        break;
    case PROTO_CMD_READ_PAGES:
        cmd_read_pages(r);
        break;
    case PROTO_CMD_ABORT:
        cmd_abort(r);
        break;
    case PROTO_CMD_ARM_WRITE:
        cmd_arm_write(r);
        break;
    case PROTO_CMD_DISARM:
        cmd_disarm(r);
        break;
    case PROTO_CMD_ERASE_BLOCK:
        cmd_erase_block(r);
        break;
    case PROTO_CMD_PROGRAM_PAGE:
        cmd_program_page(r);
        break;
    default:
        /* Includes BUS_TEST: M1 was dropped by the user (no logic analyzer; the chip-level checks covered the
         * wiring). */
        send_status(r, PROTO_ST_ERR_UNKNOWN_CMD);
        break;
    }
}

/* ---- input ----------------------------------------------------------------------------------------------- */

static uint32_t now_ms(void) { return to_ms_since_boot(get_absolute_time()); }

/* Received bytes not yet fed to the parser. Shared, not local: the READ_PAGES stream calls poll_input() from inside
 * a dispatch, and the nested call must carry on from the same byte so requests are parsed in arrival order. */
static uint8_t rx_buf[64];
static uint32_t rx_len, rx_pos;

/* Read pending bytes and hand every complete, CRC-valid request to on_req, parsed into *req. A bad CRC gets ERR_CRC
 * here. */
static void poll_input(void (*on_req)(const proto_req_t *r), proto_req_t *req) {
    if (!frame_parser_idle(&parser) && now_ms() - last_rx_ms > PROTO_REQ_TIMEOUT_MS)
        frame_parser_reset(&parser); /* stale partial request */

    for (;;) {
        if (rx_pos == rx_len) {
            if (!tud_cdc_available())
                return;
            rx_len = tud_cdc_read(rx_buf, sizeof rx_buf);
            rx_pos = 0;
            last_rx_ms = now_ms(); /* fresh: a dispatch may have blocked on USB for a while */
            continue;
        }
        switch (frame_parser_feed(&parser, rx_buf[rx_pos++], req)) {
        case FRAME_OK:
            on_req(req);
            break;
        case FRAME_BAD_CRC:
            send_status(req, PROTO_ST_ERR_CRC);
            break;
        case FRAME_NEED_MORE:
            break;
        }
    }
}

void protocol_poll(void) {
    static proto_req_t req; /* static: 2 KB, too big for the 2 KB main stack */
    poll_input(dispatch, &req);
}

/* DTR changed: the host opened or closed the port. Whatever is left from the previous session is stale: a partly
 * sent response in the TX FIFO (TinyUSB only clears it on bus reset), unread request bytes, a partial request. */
void tud_cdc_line_state_cb(uint8_t itf, bool dtr, bool rts) {
    (void)itf;
    (void)rts;
    static bool last_dtr;
    if (dtr == last_dtr)
        return; /* RTS-only change */
    last_dtr = dtr;
    session++; /* makes an in-progress usb_write_all() give up instead of finishing an old frame */
    nand_write_disarm(); /* PROTOCOL "Write mode": a new or closed session is never armed */
    tud_cdc_write_clear();
    tud_cdc_read_flush();
    rx_len = rx_pos = 0;
    frame_parser_reset(&parser);
}

/* Pico SDK convention: setting 1200 baud reboots into BOOTSEL (handy for reflashing; pyserial never uses it). */
void tud_cdc_line_coding_cb(uint8_t itf, cdc_line_coding_t const *coding) {
    (void)itf;
    if (coding->bit_rate == 1200)
        reset_usb_boot(0, 0);
}
