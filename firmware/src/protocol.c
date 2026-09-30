#include "protocol.h"

#include <string.h>

#include "crc32.h"
#include "frame.h"
#include "hardware/clocks.h"
#include "pico/bootrom.h"
#include "pico/time.h"
#include "tusb.h"
#include "version.h"

static frame_parser_t parser;
static uint32_t last_rx_ms;
static timing_t timing;
static uint8_t timing_mode;

void protocol_init(void) {
    frame_parser_reset(&parser);
    timing_preset_default(&timing);
    timing_mode = PROTO_TIMING_DEFAULT;
}

const timing_t *protocol_timing(void) { return &timing; }

/* ---- output ---------------------------------------------------------------------------------------------- */

/* Write everything, running tud_task() while the TX FIFO is full: the device never drops bytes and never overruns
 * (PROTOCOL "Transport"). Gives up only if the host goes away (DTR drops), so it can't block forever. */
static bool usb_write_all(const uint8_t *p, uint32_t n) {
    while (n) {
        if (!tud_cdc_connected())
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

    bool ok = usb_write_all(hdr, sizeof hdr) && usb_write_all(payload, len) && usb_write_all(tail, sizeof tail);
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

static void cmd_abort(const proto_req_t *r) {
    /* No stream is running outside READ_PAGES, so ABORT here is a no-op. */
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
    case PROTO_CMD_ABORT:
        cmd_abort(r);
        break;
    default:
        /* Includes NAND commands not implemented yet in this milestone (M0). */
        send_status(r, PROTO_ST_ERR_UNKNOWN_CMD);
        break;
    }
}

/* ---- input ----------------------------------------------------------------------------------------------- */

void protocol_poll(void) {
    uint32_t now = to_ms_since_boot(get_absolute_time());
    if (!frame_parser_idle(&parser) && now - last_rx_ms > PROTO_REQ_TIMEOUT_MS)
        frame_parser_reset(&parser); /* stale partial request */

    while (tud_cdc_available()) {
        uint8_t buf[64];
        uint32_t n = tud_cdc_read(buf, sizeof buf);
        last_rx_ms = now;
        for (uint32_t i = 0; i < n; i++) {
            proto_req_t req;
            switch (frame_parser_feed(&parser, buf[i], &req)) {
            case FRAME_OK:
                dispatch(&req);
                break;
            case FRAME_BAD_CRC:
                send_status(&req, PROTO_ST_ERR_CRC);
                break;
            case FRAME_NEED_MORE:
                break;
            }
        }
    }
}

/* Pico SDK convention: setting 1200 baud reboots into BOOTSEL (handy for reflashing; pyserial never uses it). */
void tud_cdc_line_coding_cb(uint8_t itf, cdc_line_coding_t const *coding) {
    (void)itf;
    if (coding->bit_rate == 1200)
        reset_usb_boot(0, 0);
}
