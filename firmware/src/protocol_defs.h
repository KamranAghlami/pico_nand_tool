/*
 * Wire protocol constants. Canonical definition: docs/PROTOCOL.md.
 * Mirrored by host/nand_tool/protocol.py; host/tests/test_protocol.py checks the two agree.
 * Keep every value a plain literal so that test can parse this file.
 */
#ifndef PROTOCOL_DEFS_H
#define PROTOCOL_DEFS_H

#define PROTO_VERSION 1

#define PROTO_MAGIC_REQ 0xA5
#define PROTO_MAGIC_RESP 0x5A

#define PROTO_REQ_HDR_LEN 4
#define PROTO_RESP_HDR_LEN 10
#define PROTO_CRC_LEN 4
#define PROTO_MAX_ARGS 32
#define PROTO_MAX_PAYLOAD 2112
#define PROTO_REQ_TIMEOUT_MS 100

#define PROTO_PAGE_NONE 0xFFFFFFFF
#define PROTO_END_FLAG 0x80

/* Commands */
#define PROTO_CMD_PING 0x01
#define PROTO_CMD_BUS_TEST 0x02
#define PROTO_CMD_SET_TIMING 0x03
#define PROTO_CMD_RESET 0x04
#define PROTO_CMD_READ_ID 0x05
#define PROTO_CMD_READ_STATUS 0x06
#define PROTO_CMD_READ_PARAM 0x07
#define PROTO_CMD_READ_PAGES 0x08
#define PROTO_CMD_ABORT 0x09

/* Status codes */
#define PROTO_ST_OK 0x00
#define PROTO_ST_ERR_CRC 0x01
#define PROTO_ST_ERR_UNKNOWN_CMD 0x02
#define PROTO_ST_ERR_BAD_ARGS 0x03
#define PROTO_ST_ERR_RB_TIMEOUT 0x04
#define PROTO_ST_ERR_ABORTED 0x05
#define PROTO_ST_ERR_BUSY 0x06
#define PROTO_ST_ERR_TIMING_FLOOR 0x07

/* SET_TIMING modes */
#define PROTO_TIMING_QUERY 0
#define PROTO_TIMING_DEFAULT 1
#define PROTO_TIMING_SLOW 2
#define PROTO_TIMING_CUSTOM 3

/* timing_t wire layout: PROTO_TIMING_FIELDS x u16 cycles, then u32 rb_timeout_us */
#define PROTO_TIMING_FIELDS 11
#define PROTO_TIMING_WIRE_LEN 26

/* Presets (docs/PROTOCOL.md "timing_t"). Field order: t_cs t_setup t_wp t_wh t_whr t_rea t_reh t_rhw t_wb t_rr t_ceh */
#define PROTO_TIMING_DEFAULT_CYCLES { 6, 3, 4, 3, 15, 8, 3, 25, 25, 6, 8 }
#define PROTO_TIMING_SLOW_CYCLES 125
#define PROTO_TIMING_RB_TIMEOUT_US 1000
#define PROTO_TIMING_RB_TIMEOUT_MAX_US 100000

#endif
