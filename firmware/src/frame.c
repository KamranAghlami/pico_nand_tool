#include "frame.h"

#include <string.h>

#include "crc32.h"

void frame_parser_reset(frame_parser_t *p) { p->len = 0; }

/* Drop buf[0] (a false magic) and rescan the remaining bytes. They are fewer than a full header, so no frame can
 * complete here.
 *
 * Why rescan: a stray A5 byte (line noise, the tail of an earlier corrupted request) looks like a frame start. If
 * the parser just threw away all 5 buffered bytes, it could throw away the real A5 of the next request with them.
 * Re-feeding the bytes after the false magic means a real frame start among them is found again. Example: a stray
 * A5, then the real request A5 0D 07 44 08 ... (PROGRAM_PAGE, seq 7, arg_len 0x0844 = 2116). The first header seen
 * is A5 A5 0D 07 44, i.e. arg_len 0x4407 > 2116, so that first A5 was false: drop it and re-feed A5 0D 07 44, and
 * the real frame carries on as if nothing happened. If a false header happens to give a plausible arg_len, the CRC
 * catches it instead (ERR_CRC) and the host retries. */
static void frame_parser_resync(frame_parser_t *p) {
    uint8_t rest[PROTO_REQ_HDR_LEN];
    uint16_t n = (uint16_t)(p->len - 1);
    memcpy(rest, &p->buf[1], n);
    p->len = 0;
    for (uint16_t i = 0; i < n; i++)
        (void)frame_parser_feed(p, rest[i], NULL); /* out is only written when a frame completes */
}

/* The parser's whole state is p->len, the number of bytes buffered:
 *   0                    hunting for the magic byte; anything else is skipped
 *   1 .. header-1        collecting the 5-byte header
 *   header .. full-1     arg_len is known, so the total frame length is known; collecting args + CRC
 *   full                 check the CRC, hand out the request, back to 0 */
frame_result_t frame_parser_feed(frame_parser_t *p, uint8_t byte, proto_req_t *out) {
    if (p->len == 0 && byte != PROTO_MAGIC_REQ)
        return FRAME_NEED_MORE;
    p->buf[p->len++] = byte;

    if (p->len < PROTO_REQ_HDR_LEN)
        return FRAME_NEED_MORE;
    uint16_t arg_len = get_le16(&p->buf[3]);
    if (arg_len > PROTO_MAX_ARGS) {
        frame_parser_resync(p);
        return FRAME_NEED_MORE;
    }
    unsigned body = PROTO_REQ_HDR_LEN + arg_len;
    if (p->len < body + PROTO_CRC_LEN)
        return FRAME_NEED_MORE;

    out->cmd = p->buf[1];
    out->seq = p->buf[2];
    out->arg_len = arg_len;
    memcpy(out->args, &p->buf[PROTO_REQ_HDR_LEN], arg_len);
    bool crc_ok = crc32(p->buf, body) == get_le32(&p->buf[body]);
    p->len = 0;
    return crc_ok ? FRAME_OK : FRAME_BAD_CRC;
}

void frame_resp_header(uint8_t hdr[PROTO_RESP_HDR_LEN], uint8_t cmd, uint8_t seq, uint8_t status, uint32_t page,
                       uint16_t payload_len) {
    hdr[0] = PROTO_MAGIC_RESP;
    hdr[1] = cmd;
    hdr[2] = seq;
    hdr[3] = status;
    put_le32(&hdr[4], page);
    put_le16(&hdr[8], payload_len);
}

unsigned frame_req_encode(uint8_t out[FRAME_REQ_MAX], uint8_t cmd, uint8_t seq, const uint8_t *args,
                          uint16_t arg_len) {
    out[0] = PROTO_MAGIC_REQ;
    out[1] = cmd;
    out[2] = seq;
    put_le16(&out[3], arg_len);
    if (arg_len)
        memcpy(&out[PROTO_REQ_HDR_LEN], args, arg_len);
    unsigned body = PROTO_REQ_HDR_LEN + arg_len;
    put_le32(&out[body], crc32(out, body));
    return body + PROTO_CRC_LEN;
}
