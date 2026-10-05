/* Request parsing and response header encoding (docs/PROTOCOL.md). Pure C, no SDK: unit-tested on the host. */
#ifndef FRAME_H
#define FRAME_H

#include <stdbool.h>
#include <stdint.h>

#include "protocol_defs.h"

#define FRAME_REQ_MAX (PROTO_REQ_HDR_LEN + PROTO_MAX_ARGS + PROTO_CRC_LEN)

typedef struct {
    uint8_t cmd;
    uint8_t seq;
    uint16_t arg_len;
    uint8_t args[PROTO_MAX_ARGS];
} proto_req_t; /* 2 KB: keep instances static, never on the 2 KB main stack */

typedef enum {
    FRAME_NEED_MORE, /* no complete frame yet */
    FRAME_OK,        /* *out holds a valid request */
    FRAME_BAD_CRC,   /* *out holds cmd/seq as received; do not execute */
} frame_result_t;

typedef struct {
    uint8_t buf[FRAME_REQ_MAX];
    uint16_t len;
} frame_parser_t;

void frame_parser_reset(frame_parser_t *p);
static inline bool frame_parser_idle(const frame_parser_t *p) { return p->len == 0; }

/* Feed one received byte. Skips bytes until PROTO_MAGIC_REQ; an arg_len > PROTO_MAX_ARGS is a false magic and
 * scanning resumes from the byte after it. */
frame_result_t frame_parser_feed(frame_parser_t *p, uint8_t byte, proto_req_t *out);

/* Fill the fixed 10-byte response header. */
void frame_resp_header(uint8_t hdr[PROTO_RESP_HDR_LEN], uint8_t cmd, uint8_t seq, uint8_t status, uint32_t page,
                       uint16_t payload_len);

/* Encode a complete request (used by host-side tests). Returns the frame length. */
unsigned frame_req_encode(uint8_t out[FRAME_REQ_MAX], uint8_t cmd, uint8_t seq, const uint8_t *args,
                          uint16_t arg_len);

static inline void put_le16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}
static inline void put_le32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}
static inline uint16_t get_le16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static inline uint32_t get_le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

#endif
