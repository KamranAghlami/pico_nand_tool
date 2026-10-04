#include "protocol.h"

#include <string.h>

#include "crc32.h"
#include "frame.h"
#include "hardware/clocks.h"
#include "nand_ops.h"
#include "pico/bootrom.h"
#include "pico/time.h"
#include "tusb.h"
#include "version.h"

static frame_parser_t parser;
static uint32_t last_rx_ms;
static timing_t timing;
static uint8_t timing_mode;
static volatile uint32_t session; /* bumped on every DTR change (tud_cdc_line_state_cb) */

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
            continue;
        }
        uint32_t k = tud_cdc_write(p, n < avail ? n : avail);
        p += k;
        n -= k;
    }
    return true;
}

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
    uint32_t busy_cycles = 0;
    uint8_t st = nand_reset(&busy_cycles);
    if (st != PROTO_ST_OK) {
        send_status(r, st);
        return;
    }
    uint8_t buf[4];
    put_le32(buf, (uint32_t)((uint64_t)busy_cycles * 1000000000u / clock_get_hz(clk_sys)));
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

/* ---- READ_PAGES stream (docs/PROTOCOL.md "READ_PAGES stream") --------------------------------------------- */

static void poll_input(void (*on_req)(const proto_req_t *r));

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
        tud_task();
        poll_input(stream_on_req);
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
    default:
        /* Includes commands not implemented yet at this milestone (M4): BUS_TEST. */
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

/* Read pending bytes and hand every complete, CRC-valid request to on_req. A bad CRC gets ERR_CRC here. */
static void poll_input(void (*on_req)(const proto_req_t *r)) {
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
        proto_req_t req;
        switch (frame_parser_feed(&parser, rx_buf[rx_pos++], &req)) {
        case FRAME_OK:
            on_req(&req);
            break;
        case FRAME_BAD_CRC:
            send_status(&req, PROTO_ST_ERR_CRC);
            break;
        case FRAME_NEED_MORE:
            break;
        }
    }
}

void protocol_poll(void) { poll_input(dispatch); }

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
