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

#define PROTO_PARAM_LEN 768 /* READ_PARAM payload: 3 x 256-byte parameter page copies */

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

/* SET_TIMING floors (docs/PROTOCOL.md floors table): datasheet Table 20 values in ns, checked against sums of
 * timing_t phases. Also used by host/nand_tool/protocol.py Timing.meets_floors(). */
#define PROTO_FLOOR_TCS_NS 20      /* t_cs + t_setup + t_wp        */
#define PROTO_FLOOR_TCR_NS 10      /* t_cs                         */
#define PROTO_FLOOR_TSETUP_NS 10   /* t_setup + t_wp: tCLS/tALS/tDS */
#define PROTO_FLOOR_TWP_NS 12      /* t_wp                         */
#define PROTO_FLOOR_THOLD_NS 5     /* t_wh: tCLH/tALH/tDH/tCH       */
#define PROTO_FLOOR_TWH_NS 10      /* t_wh + t_setup               */
#define PROTO_FLOOR_TWC_NS 25      /* t_setup + t_wp + t_wh        */
#define PROTO_FLOOR_TWHR_NS 60     /* t_whr                        */
#define PROTO_FLOOR_TREA_NS 20     /* t_rea, plus input sync       */
#define PROTO_FLOOR_TREH_NS 10     /* t_reh                        */
#define PROTO_FLOOR_TRC_NS 25      /* t_rea + t_reh                */
#define PROTO_FLOOR_TRHW_NS 100    /* t_rhw                        */
#define PROTO_FLOOR_TWB_NS 100     /* t_wb                         */
#define PROTO_FLOOR_TRR_NS 20      /* t_rr                         */
#define PROTO_FLOOR_TCHZ_NS 30     /* t_ceh                        */
#define PROTO_FLOOR_INPUT_SYNC_CYCLES 2  /* RP2040 GPIO input synchronizer, added to the t_rea floor */
#define PROTO_FLOOR_RB_TIMEOUT_US 25     /* tR                     */

#endif
