# Wire Protocol (canonical)

Protocol version **2** (2026-10-05: write mode, see `WRITE_PROPOSAL.md` §4). Version 1 was approved 2026-09-30 (see
`PROPOSAL.md` §3). This file is the source of truth. Change it here
first, then change `firmware/src/protocol_defs.h` and `host/nand_tool/protocol.py` together. A pytest checks that
the two agree.

## Transport

- USB full-speed, TinyUSB **CDC-ACM used as a raw byte pipe**. No `stdio_usb`, no text on this channel.
- VID:PID `2E8A:000A`, manufacturer `"Raspberry Pi"`, product `"Pico NAND Tool"`, serial = flash unique ID.
- Line coding (baud etc.) is ignored. The device aborts a running stream when DTR drops (host closed the port).
- Setting the line coding to **1200 baud** reboots the Pico into BOOTSEL for reflashing (Pico SDK convention;
  the host tool never uses 1200 baud).
- **DTR change** (host opens or closes the port): the device discards the previous session's leftovers, i.e. any
  queued response bytes, unread request bytes and a partial request. A response write in progress is abandoned.
- Host auto-detect: VID:PID plus product string. If the OS doesn't report the product string (Windows), the host
  never guesses, because stock Pico SDK USB-serial firmware has the same VID:PID. The user passes `--port`. The host
  opens the port exclusively (POSIX) and discards input on open. Its first `seq` is random.
- Flow control: the device only writes what fits in the TinyUSB TX FIFO. If the host stops reading, the device waits.
  It never drops data.

## Conventions

- All integers are **little-endian**.
- **CRC-32** = IEEE 802.3 / zlib: reflected poly `0xEDB88320`, init `0xFFFFFFFF`, final XOR `0xFFFFFFFF`.
  Check value: `crc32("123456789") = 0xCBF43926`. On the host this is `binascii.crc32`.

## Request frame (host → device)

| Off | Size | Field | Value |
|---:|---:|---|---|
| 0 | 1 | magic | `0xA5` |
| 1 | 1 | cmd | §Commands |
| 2 | 1 | seq | host-chosen, echoed in every response to this request |
| 3 | 2 | arg_len | u16, 0 … 2116 |
| 5 | n | args | |
| 5+n | 4 | crc32 | over bytes `[0, 5+n)` |

- The device skips bytes until it sees `0xA5`. `arg_len > 2116` → the byte is treated as a false magic and scanning
  resumes. (v1 had a 1-byte `arg_len` ≤ 32; v2 widened it so `PROGRAM_PAGE` can carry a whole page.)
- If more than **100 ms** pass between bytes of one request, the partial request is discarded.
- A bad CRC gets a response with `status = ERR_CRC` (cmd/seq echoed as received), and the command is **not**
  executed.

## Response frame (device → host)

| Off | Size | Field | Value |
|---:|---:|---|---|
| 0 | 1 | magic | `0x5A` |
| 1 | 1 | cmd | request cmd; **bit 7 set = end-of-stream frame** |
| 2 | 1 | seq | request seq |
| 3 | 1 | status | §Status codes |
| 4 | 4 | page | page index, or `0xFFFFFFFF` (`PAGE_NONE`) |
| 8 | 2 | len | payload length, 0 … 2112 |
| 10 | n | payload | |
| 10+n | 4 | crc32 | over bytes `[0, 10+n)` |

Every request gets exactly one response frame, except `READ_PAGES`, which gets `count` page frames plus one end frame.

## Status codes

| Code | Name | Meaning |
|---:|---|---|
| `0x00` | `OK` | |
| `0x01` | `ERR_CRC` | request CRC mismatch; nothing executed |
| `0x02` | `ERR_UNKNOWN_CMD` | command not known (or not implemented in this firmware build) |
| `0x03` | `ERR_BAD_ARGS` | wrong `arg_len`, value out of range, disallowed READ_ID address |
| `0x04` | `ERR_RB_TIMEOUT` | R/B# not high within `rb_timeout_us` (10 ms for the pre-RESET power-on wait) |
| `0x05` | `ERR_ABORTED` | stream stopped by `ABORT` or DTR drop |
| `0x06` | `ERR_BUSY` | request (other than `ABORT`) received while a stream is running |
| `0x07` | `ERR_TIMING_FLOOR` | `SET_TIMING` custom value below the datasheet floor; nothing changed |
| `0x08` | `ERR_NOT_ARMED` | erase/program while write mode is not armed, the arm timed out, or the block is outside the armed range; nothing reached the bus |
| `0x09` | `ERR_BAD_BLOCK` | `ERASE_BLOCK` refused: the block's factory bad-block marker is not `FFh` (§9.2); nothing erased |
| `0x0A` | `ERR_OP_FAILED` | program/erase finished with SR bit 0 = 1 (fail) or bit 6 = 0; the block is grown-bad (§9.1) |
| `0x0B` | `ERR_WP_STUCK` | after program/erase SR bit 7 = 0: WP# never went high, so the chip ignored the command (§2.5) |

## Commands

| Code | Name | Args | Response payload |
|---:|---|---|---|
| `0x01` | `PING` | none | `u8 proto_ver`, `u8 fw_major`, `u8 fw_minor`, `u8 fw_patch`, `u32 clk_sys_hz`, then an ASCII version string (no NUL), e.g. `pico-nand-tool 0.1.0 (g1a2b3c4)` |
| `0x02` | `BUS_TEST` | none, or `u16 step_us` (0 → 10) | 3 bytes, see §BUS_TEST |
| `0x03` | `SET_TIMING` | `u8 mode` (0 = query, 1 = DEFAULT, 2 = SLOW), or `u8 mode=3` + `timing_t` (30 B) | `u8 active_mode` (1/2/3) + active `timing_t` (31 B total) |
| `0x04` | `RESET` | none | `u32 busy_ns`: measured R/B# low time after `FFh`, 0 = never seen low. Also disarms write mode |
| `0x05` | `READ_ID` | none (→ `00h`, 5), or `u8 addr` ∈ {`00h`, `20h`}, `u8 n` ∈ 1…8 | `n` bytes |
| `0x06` | `READ_STATUS` | none | 1 byte: status register (sent as `00h` then `70h`) |
| `0x07` | `READ_PARAM` | none | 768 bytes (3 parameter-page copies; `FFh` Reset is issued first) |
| `0x08` | `READ_PAGES` | `u32 start`, `u32 count`; `count ≥ 1`, `start + count ≤ 131072` | stream, see below |
| `0x09` | `ABORT` | none | during a stream: stops it (see below). Otherwise: empty `OK` |
| `0x0A` | `ARM_WRITE` | `u32 token` = `0x4D524157` (bytes `57 41 52 4D`, "WARM"), `u16 first_block`, `u16 last_block`, `u16 idle_timeout_s` | empty `OK`. See §Write mode |
| `0x0B` | `DISARM` | none | empty `OK` (also when not armed) |
| `0x0C` | `ERASE_BLOCK` | `u16 block`, `u8 flags` (bit 0 = ignore the bad-block marker; other bits 0) | `u8 sr`, `u32 busy_ns`; `page` = first page of the block |
| `0x0D` | `PROGRAM_PAGE` | `u32 page`, then 2112 bytes (2048 data + 64 spare) | `u8 sr`, `u32 busy_ns`; `page` = the page |

### READ_PAGES stream

- For each page `p` in `[start, start+count)`, one **page frame**: `cmd = 0x08`, `page = p`, and either
  - `status = OK`, `len = 2112` (2048 data + 64 spare, column 0 upward), or
  - `status = ERR_RB_TIMEOUT`, `len = 0`. The device issues `FFh` to recover and continues with `p + 1`.
- Then one **end frame**: `cmd = 0x88`, `status` = `OK` or `ERR_ABORTED`, `page` = the first page index *not* sent,
  and a payload of `u32 pages_sent, u32 pages_failed` (8 B). `pages_sent` counts page frames of either status.
- Between pages the device parses incoming requests. `ABORT` ends the stream after the current page: the end frame
  carries the stream's seq and `ERR_ABORTED`, followed by the `ABORT` request's own `OK` response. Any other valid
  request gets an immediate `ERR_BUSY` response with its own seq, interleaved between page frames. That includes a
  second `ABORT`, an `ABORT` with arguments, and another `READ_PAGES`; a request with a bad CRC still gets `ERR_CRC`.
- If the host closes the port (DTR drops) the stream stops at once, without an end frame.

### Host recovery rule

On any frame error (bad magic, bad CRC, `len` out of range, unexpected `seq`, `page` out of order), the host sends
`ABORT`. It then discards input until it has seen the end frame for the stream's seq, or 250 ms of silence. After
that it re-issues `READ_PAGES` from the first page it did not accept. It never splices bytes across a framing error.
The same rule applies to single-response commands: `ABORT`, drain, retry. The drain is bounded (3 s by default). A
line that never goes quiet is a hard error ("not a Pico NAND Tool?"), not an endless wait. Every read also honours
the per-request deadline while bytes keep arriving, so foreign or stale frames can't stall a request.

## `timing_t` (30 bytes)

Thirteen `u16` delays in **clk_sys cycles** (`clk_sys_hz` is in `PING`; 125 MHz → 8 ns), then `u32 rb_timeout_us`.
Each delay is a minimum. See `PROPOSAL.md` §4.2 for the datasheet derivation.

| # | Field | DEFAULT | SLOW |
|---:|---|---:|---:|
| 0 | `t_cs` | 6 | 125 |
| 1 | `t_setup` | 3 | 125 |
| 2 | `t_wp` | 4 | 125 |
| 3 | `t_wh` | 3 | 125 |
| 4 | `t_whr` | 15 | 125 |
| 5 | `t_rea` | 8 | 125 |
| 6 | `t_reh` | 3 | 125 |
| 7 | `t_rhw` | 25 | 125 |
| 8 | `t_wb` | 25 | 125 |
| 9 | `t_rr` | 6 | 125 |
| 10 | `t_ceh` | 8 | 125 |
| 11 | `t_adl` | 18 | 125 |
| 12 | `t_ww` | 25 | 125 |
| — | `rb_timeout_us` (u32) | 1000 | 1000 |

**Floors**, checked on `SET_TIMING` mode 3 (ns → cycles, rounded up). Every Table 20 constraint is checked against
the sum of the phases that make it up. The ns values are the `PROTO_FLOOR_*` constants, which firmware and host
share:

| Constraint (Table 20) | Checked sum | Floor |
|---|---|---|
| tCS 20 (CE#↓ → WE#↑) | `t_cs + t_setup + t_wp` | ≥ 20 ns |
| tCR 10 | `t_cs` | ≥ 10 ns |
| tCLS/tALS/tDS 10 (→ WE#↑) | `t_setup + t_wp` | ≥ 10 ns |
| tWP 12 | `t_wp` | ≥ 12 ns |
| tCLH/tALH/tDH/tCH 5 | `t_wh` | ≥ 5 ns |
| tWH 10 | `t_wh + t_setup` | ≥ 10 ns |
| tWC 25 | `t_setup + t_wp + t_wh` | ≥ 25 ns |
| tWHR 60, tAR 10, tCLR 10 | `t_whr` | ≥ 60 ns |
| tREA 20 max + 2-cycle input sync; tRP 12 | `t_rea` | ≥ 20 ns + 2 cycles |
| tREH 10 | `t_reh` | ≥ 10 ns |
| tRC 25 | `t_rea + t_reh` | ≥ 25 ns |
| tRHW 100, tRHZ 100 max | `t_rhw` | ≥ 100 ns |
| tWB 100 max | `t_wb` | ≥ 100 ns |
| tRR 20 | `t_rr` | ≥ 20 ns |
| tCHZ 30 max, tCSD 10 | `t_ceh` | ≥ 30 ns |
| tADL 70 (last address WE#↑ → first data WE#↑) | `t_adl` | ≥ 70 ns |
| tWW 100 (WP# → WE#↑ of `80h`/`60h`) | `t_ww` | ≥ 100 ns |
| tR 25 µs | `rb_timeout_us` | ≥ 25 µs |

`rb_timeout_us` is also capped at 100 000, so a command can never hang for long. It applies to reads only: program
and erase have fixed R/B# timeouts of 2 000 µs (tPROG ≤ 700 µs) and 20 000 µs (tBERS ≤ 10 ms), Table 23.

## BUS_TEST

Chip-safe: CE# stays **high** throughout, except step 1, which has no strobes and CLE = ALE = 0. With CE# high the
chip ignores the bus and its I/Os are high-Z (datasheet Table 3). One step = `step_us`.

| Step | Action |
|---:|---|
| 0 | idle: CE# = WE# = RE# = 1, CLE = ALE = 0, IO = output `00h`; hold 10 steps (start marker) |
| 1 | CE# low 1 step, high 1 step |
| 2 | CLE high 1 step, low 1 step |
| 3 | ALE high 1 step, low 1 step |
| 4 | WE# low 1 step, high 1 step |
| 5 | RE# low 1 step, high 1 step |
| 6 | IO = `01h, 02h, 04h … 80h` (walking one), each 1 step, then `00h` 1 step |
| 7 | IO = `FFh, 00h, 55h, AAh`, each 1 step, then `00h` 1 step |
| 8 | IO → input, idle 10 steps (end marker) |

Then the readback (not visible on the analyzer, apart from the IO lines drifting):

| Byte | Meaning | Expected (no chip, or chip deselected) |
|---:|---|---|
| 0 | IO[7:0] read with internal **pull-ups** on the bus, after 20 µs settle | `FFh` |
| 1 | IO[7:0] read with internal **pull-downs** on the bus, after 20 µs settle | `00h` |
| 2 | bit 0 = R/B# level (external pull-up only); bits 7:1 = 0 | `01h` |

A bit stuck against its pull, in byte 0 or 1, means a shorted or externally loaded IO line. After the readback the
bus is left as an input with pull-downs (the idle state).

## Write mode

Write mode is **off at boot**. Erase and program are refused with `ERR_NOT_ARMED`, without touching the bus, unless
the device is armed and the block lies in the armed range.

- `ARM_WRITE` checks: token `0x4D524157`, `first_block ≤ last_block ≤ 2047`, `1 ≤ idle_timeout_s ≤ 60`; otherwise
  `ERR_BAD_ARGS` and the previous arm state is kept. A valid `ARM_WRITE` replaces any earlier arm.
- The arm is dropped by `DISARM`, `RESET`, a DTR change (port opened or closed), `idle_timeout_s` passing since the
  arm or the last successful erase/program, and any erase/program that ends in `ERR_RB_TIMEOUT`, `ERR_OP_FAILED` or
  `ERR_WP_STUCK`. `ERR_BAD_ARGS`, `ERR_NOT_ARMED` and `ERR_BAD_BLOCK` leave it as it is.
- Erase and program are single-response commands (no stream). During a `READ_PAGES` stream they get `ERR_BUSY`.
- **The host must not re-send an erase or program after a lost response**: it cannot know whether it ran. The host
  tool re-does the whole block instead (erase, program, and verify if asked). An `ERR_CRC` response is safe to retry:
  nothing ran.

Check order: `ERR_BAD_ARGS` (lengths, block < 2048, page < 131072, flags) → `ERR_NOT_ARMED` → (erase only) marker
check → the operation.

### ERASE_BLOCK (§3.5, Fig. 24)

1. Unless flags bit 0 is set: read spare byte 0 (column 2048) of pages 0, 1 and 63 of the block (`00h`/`30h` reads).
   Any value other than `FFh` → `ERR_BAD_BLOCK`. An R/B# timeout here → `ERR_RB_TIMEOUT`.
2. CE# low, WP# high, `t_ww`, `60h`, 3 row-address cycles (row = block × 64), `D0h`, `t_wb`, poll R/B# for up to
   20 ms, `70h`, read SR, WP# low, CE# high.
3. SR checks in this order: bit 7 = 0 → `ERR_WP_STUCK`; bit 0 = 1 or bit 6 = 0 → `ERR_OP_FAILED`; otherwise `OK`.
   These three carry the `sr` + `busy_ns` payload.
4. R/B# timeout: WP# goes low at once (that aborts the operation, §4.3), then `FFh` (Reset) recovers the chip →
   `ERR_RB_TIMEOUT` (empty payload). The block's content is undefined until it is erased again (§3.5).

### PROGRAM_PAGE (§3.2, Fig. 19)

CE# low, WP# high, `t_ww`, `80h`, 5 address cycles (column 0, row = page), `t_adl`, 2112 data-input cycles (WE#
with CLE = ALE = 0; `t_setup`, `t_wp`, `t_wh` per byte), `10h`, `t_wb`, poll R/B# for up to 2 ms, `70h`, read SR,
WP# low, CE# high. SR checks and timeout handling as for erase. The device does not check that the page was erased;
the host does. Each page may be programmed at most 4 times between erases (NOP, Table 23); the host tool programs
each page once.
