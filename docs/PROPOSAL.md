# Design Proposal (approved)

Status: **APPROVED by the user on 2026-09-30.** This is now a design record. The canonical wire protocol is
`docs/PROTOCOL.md`; if the two ever differ, `PROTOCOL.md` wins.
Covers: (1) datasheet cross-check of `SPEC.md`, (2) repo layout, (3) USB transport and protocol
byte format, (4) default timing values.

All datasheet references are to *S34ML01G1/S34ML02G1/S34ML04G1, doc 002-00676 Rev \*W* (`docs/datasheet.pdf`).

---

## 1. Datasheet cross-check of SPEC.md

I checked every chip fact and timing in `SPEC.md` against the datasheet. **Nothing conflicts.**

| SPEC item | Datasheet | Result |
|---|---|---|
| READ ID `01 DA 90 95 44` | Table 14, Fig. 41 (2 Gb ×8) | ✅ |
| 2112 B page, 64 pages/block, 2 × 1024 blocks | §1.5 Fig. 6, Table 1 | ✅ |
| 5 address cycles, 2 col + 3 row, param byte 101 = `23h` | §2.2, Table 5, Table 3.4 | ✅ |
| Row = page index; block = page >> 6 | Table 5: R1 = PA0–5, PLA0 (A18), BA0; R2 = BA1–BA8; R3 bit0 = BA9 | ✅ (the plane bit is block bit 0, so even blocks are plane 0 and odd blocks plane 1) |
| tR ≤ 25 µs, tRST ≤ 5 µs from ready | Table 20 and note 29 | ✅ |
| Max bad blocks 40 (`28h 00h`) | Table 3.4 bytes 103–104; Table 17 (N_VB ≥ 2008) | ✅ |
| Param CRC bytes 254–255 = `3B C5` | Table 3.4 (S34ML02G100 ×8) | ✅ I **rebuilt the full 256-byte parameter page from Table 3.4** and ran the specified ONFI CRC-16 over it. The result is `0xC53B`, stored as `3B C5`. This confirms the algorithm, and the rebuilt page becomes the reference page for the fake device and the tests. |
| FFh before ECh (41 nm 2 Gb) | §3.19 note | ✅ |
| Bad-block rule (1st, 2nd or last page, spare byte 0 ≠ FFh) | §9.2 and Fig. 55 note 84 | ✅ |
| All Table 20 values | Table 20 (checked against a render of p.35) | ✅ |
| Pin map balls | Fig. 3 (checked against a render of p.6) | ✅ |

### Details the spec leaves out (they shape the design and need no spec change)

1. **Expected status after RESET is `60h`, not `E0h`** (§3.12). WP# is grounded, so SR bit 7 (write-protect) reads `0` = "protected".
   The host tool decodes this. **If it ever reads `E0h`, WP# is not really grounded: stop and check the hardware.**
   This gives M2 a free hardware safety check.
2. **Read ID followed by Read Status needs a dummy `00h`** in between (§3.16 note). `00h` is already on the allowed opcode
   list. Proposal: `READ_STATUS` always issues `00h` and then `70h`. This means no hidden "what did we do last" state, and
   `00h` alone is harmless.
3. **After power-up the chip is busy for up to 5 ms and ignores every command except `70h`** (§4.1, Fig. 46). The Pico
   and the NAND share 3V3, so the chip powers up together with the firmware. `RESET` therefore first waits for R/B#
   high with a **10 ms** timeout, then issues `FFh`. Every other wait uses the normal 1 ms timeout.
4. **tRST is 5 / 10 / 500 µs** depending on whether the reset hits a ready, read-busy or program-busy chip (Table 20).
   The 1 ms timeout covers all three.
5. **Parameter page and page reads must poll R/B#, not the status register.** Status polling needs an extra `00h`
   before data out (§3.19, Fig. 6.1 note 47, Fig. 44 note 78). We poll R/B#, which is the simpler path.
6. **Balls D3, G4 (VCC) and F7 (VSS) carry note [1]:** "might not be bonded internally" (Fig. 3). The balls that are
   guaranteed bonded are **H8 and J6 (VCC)** and **C5, K3, K8 (VSS)**. Connecting all of them, as the spec says, is
   correct. Just make sure the guaranteed ones are solid and that the 100 nF cap sits close to them.
7. **Pull-up margin before firmware init:** RP2040 pads reset with a pull-down of about 50–80 kΩ. Against the 10 kΩ
   external pull-up, CE#, WE# and RE# sit at about 2.75 V worst case. VIH(min) = 0.8·VCC = 2.64 V (§5.6), so the margin
   is thin but holds. It only matters during the boot ROM window, and the chip is in its ≤5 ms power-on busy period
   then anyway. The firmware's first action is to clear the pulls and drive these lines high. Optional hardware
   hardening: 4.7 kΩ pull-ups.
8. Any `READ_ID` address other than `00h` / `20h` is rejected. `90h` + `20h` returns `"ONFI"` (§3.18). Supporting it
   costs nothing, uses an already-allowed opcode, and gives a second 4-byte integrity check at M2.

---

## 2. Repository layout

```
pico_nand_tool/
├── CLAUDE.md                     # thin entry point → imports docs/CLAUDE.md + docs/memory/MEMORY.md
├── README.md
├── .gitignore
├── .github/workflows/ci.yml      # firmware build + safety check + host pytest
├── docs/
│   ├── CLAUDE.md                 # project instructions for Claude (the real one)
│   ├── SPEC.md                   # requirements (user-owned)
│   ├── PROPOSAL.md               # this document
│   ├── PROTOCOL.md               # canonical wire protocol (written after approval, from §3 below)
│   ├── BUS_TEST.md               # documented BUS_TEST edge sequence for the logic analyzer
│   ├── datasheet.pdf
│   └── memory/                   # repo-level project memory (MEMORY.md index + one fact per file)
├── firmware/
│   ├── CMakeLists.txt
│   ├── pico_sdk_import.cmake
│   ├── src/
│   │   ├── main.c                # init, USB task loop, request dispatch
│   │   ├── pins.h                # the ONE pin-map header (all GPIO numbers + masks)
│   │   ├── nand_cmd.h            # allowed-opcode enum + NAND_CMD() static-assert gate
│   │   ├── nand_bus.c/.h         # SIO bit-bang primitives: cmd/addr latch, data read, R/B# wait
│   │   ├── nand_ops.c/.h         # datasheet sequences: reset, read_id, status, param, page read
│   │   ├── timing.c/.h           # timing struct, DEFAULT/SLOW presets, floors, delay_cycles()
│   │   ├── bus_test.c/.h         # BUS_TEST sequence (chip-safe, see §3.5)
│   │   ├── protocol.c/.h         # framing, request parser, response writer, streaming
│   │   ├── protocol_defs.h       # opcodes/status/magic/sizes — mirrored by host/…/protocol.py
│   │   ├── crc32.c/.h            # IEEE CRC-32 (zlib-compatible)
│   │   ├── usb_descriptors.c
│   │   └── tusb_config.h
│   └── tests/
│       ├── Makefile              # `make check`: host-compiled unit tests + opcode gate (no SDK)
│       ├── forbidden_opcode.c    # MUST FAIL to compile (CI proves the opcode gate works)
│       └── test_host.c           # host-compiled unit tests for crc32, frame, timing
└── host/
    ├── pyproject.toml            # package "nand_tool", console script `nandtool`, deps: pyserial; extras [test]: pytest
    ├── nand_tool/
    │   ├── __init__.py
    │   ├── cli.py                # argparse: ping, bus-test, id, status, param, read, dump, compare, reconcile, split, badblocks
    │   ├── geometry.py           # chip constants (page/oob/block sizes, counts, expected ID)
    │   ├── protocol.py           # constants + frame encode/decode (pure, no I/O)
    │   ├── transport.py          # pyserial wrapper, port auto-detect by VID/PID/product string
    │   ├── client.py             # high-level device API: ping(), read_id(), read_pages() generator, abort/resync
    │   ├── onfi.py               # onfi_crc16 (ported from Linux nand_onfi.c), param page decode
    │   ├── dump.py               # dump with retries, resume, progress/ETA
    │   ├── compare.py            # page/byte/bit diff
    │   ├── reconcile.py          # majority vote + JSON report
    │   └── postproc.py           # split, badblocks
    └── tests/
        ├── fake_device.py        # firmware protocol simulator over a synthetic NAND image
        ├── conftest.py
        ├── test_protocol.py      # includes: constants match firmware/src/protocol_defs.h
        ├── test_onfi.py          # golden param page → CRC 3B C5
        ├── test_dump.py          # retries, resume, no-overwrite, transport errors
        ├── test_compare_reconcile.py
        └── test_postproc.py
```

Notes:
- **Constants live in two places** (C header and Python module). A pytest parses `protocol_defs.h` and asserts every
  constant matches `protocol.py`, so they cannot drift. That is cheaper than generating code.
- **Safety gate proven in CI.** `firmware/tests/forbidden_opcode.c` tries `NAND_CMD(0x80)`. CI checks that it *fails*
  to compile with the expected static-assert message.
- The firmware keeps the whole bus layer in RAM (`__not_in_flash_func`) so XIP cache misses cannot add jitter. Any
  jitter would only lengthen phases, which is always safe, but deterministic timing makes logic-analyzer checks easier.

---

## 3. USB transport and protocol

### 3.1 Transport choice: TinyUSB **CDC-ACM, used raw** (not stdio_usb)

Justification: it works with plain `pyserial` and needs no driver on Linux, macOS or Windows 10+. A vendor class would
need libusb/pyusb plus WinUSB descriptors. TinyUSB's `tud_cdc_read`/`tud_cdc_write` are byte-exact: the firmware does
no line-ending translation, and pyserial opens POSIX ports in raw mode. USB full-speed CDC on RP2040 realistically
carries about 0.8–1 MB/s, which is well above the 300 KB/s target.

- `stdio_usb` / `printf` are **not linked**. There is no text on the data channel, ever.
- `CFG_TUD_CDC_TX_BUFSIZE = 8192` (about 4 page frames) and `CFG_TUD_CDC_EP_BUFSIZE = 4096`. The RP2040 DCD runs
  multi-packet transfers in the ISR, so USB keeps draining while the CPU bit-bangs the next page.
- **Device-side flow control:** the device writes only `min(len, tud_cdc_write_available())` and runs `tud_task()`
  while waiting. It never drops or overruns anything: if the host stops reading, the device just blocks, because USB
  NAKs. The device aborts a stream if the host disconnects (DTR drops).
- VID:PID `2E8A:000A` (Raspberry Pi, Pico CDC). Product string: `"Pico NAND Tool"`. The host auto-detects by product
  string and serial number, and `--port` overrides that. (A dedicated Raspberry Pi PID can come later.)
- Optional, off by default: a second CDC interface for debug logs (`-DNAND_DEBUG_CDC=ON`). It never shares the data
  channel.

### 3.2 Common conventions

- All multi-byte integers are **little-endian**.
- **CRC-32** = IEEE 802.3 / zlib (`binascii.crc32` on the host): reflected poly `0xEDB88320`, init `0xFFFFFFFF`, final
  XOR `0xFFFFFFFF`. Check value: `crc32(b"123456789") = 0xCBF43926`.
- Different magic bytes per direction, so a host that sees its own echo, or a stray byte, fails fast.

### 3.3 Request frame (host → device)

| Offset | Size | Field | Notes |
|---:|---:|---|---|
| 0 | 1 | `magic` | `0xA5` |
| 1 | 1 | `cmd` | see §3.5 |
| 2 | 1 | `seq` | host-chosen, echoed in every response frame for this request |
| 3 | 1 | `arg_len` | 0 … 32 |
| 4 | n | `args` | command-specific |
| 4+n | 4 | `crc32` | over bytes `[0, 4+n)` |

Max request size is 40 bytes. The parser resyncs on `magic` and drops a partial request after **100 ms** of
inactivity. A frame with a bad CRC gets an `ERR_CRC` response, and its command is not executed.

### 3.4 Response frame (device → host)

| Offset | Size | Field | Notes |
|---:|---:|---|---|
| 0 | 1 | `magic` | `0x5A` |
| 1 | 1 | `cmd` | echo of the request `cmd`; **bit 7 set = end-of-stream frame** (e.g. `0x88`) |
| 2 | 1 | `seq` | echo of the request `seq` |
| 3 | 1 | `status` | see §3.6 |
| 4 | 4 | `page` | page index for page frames; `0xFFFFFFFF` = n/a |
| 8 | 2 | `len` | payload length, 0 … 2112 |
| 10 | n | `payload` | |
| 10+n | 4 | `crc32` | over bytes `[0, 10+n)` (header + payload) |

A page frame is 10 + 2112 + 4 = **2126 bytes**, 0.66 % overhead. `seq` is my addition to the spec's field list: it
lets the host discard stale frames after an abort or retry without guesswork.

### 3.5 Commands

| Code | Name | Args | Response payload |
|---:|---|---|---|
| `0x01` | `PING` | — | `u8 proto_ver(=1), u8 fw_major, u8 fw_minor, u8 fw_patch, u32 clk_sys_hz`, then ASCII version string, e.g. `pico-nand-tool 0.1.0 (g1a2b3c4)` |
| `0x02` | `BUS_TEST` | `u16 step_us` (0 → 10 µs) | none (sent after the sequence completes) |
| `0x03` | `SET_TIMING` | `u8 mode`: 0 = query only, 1 = DEFAULT, 2 = SLOW, 3 = CUSTOM (+ `timing_t`, 26 B) | `u8 active_mode` + active `timing_t` (echo) |
| `0x04` | `RESET` | — | `u32 busy_ns`: measured R/B# low time, 0 = never seen low (a wiring hint) |
| `0x05` | `READ_ID` | `u8 addr` (`00h` or `20h`), `u8 n` (1…8). Empty args = `00h`, 5 | `n` bytes |
| `0x06` | `READ_STATUS` | — | 1 byte (SR) |
| `0x07` | `READ_PARAM` | — | 768 bytes (3 copies) |
| `0x08` | `READ_PAGES` | `u32 start`, `u32 count`; needs `start + count ≤ 131072` and `count ≥ 1` | `count` page frames (`cmd=0x08`, `page=i`, 2112 B), then one end frame |
| `0x09` | `ABORT` | — | stops a running stream after the current page. With no stream running: empty `OK` reply |

**READ_PAGES stream.** Each page frame has `status=OK` with a 2112-byte payload, or `ERR_RB_TIMEOUT` with an empty
payload. On a timeout the device issues `FFh` to recover and **continues with the next page**. The host re-requests the
failed pages afterwards. The stream always ends with an **end frame**: `cmd=0x88`, `status` = `OK` or `ERR_ABORTED`,
`page` = the next page index that was *not* sent, and payload `u32 pages_sent, u32 pages_failed`. While streaming, the
only request the device acts on is `ABORT`; any other request is answered with `ERR_BUSY` after the end frame.

**Host retry and resync.** On any frame error (bad CRC, bad magic, length out of range, or a `seq`/`page` out of
order), the host sends `ABORT` and discards input until it sees the end frame with the matching `seq` (or 250 ms of
silence). It then re-issues `READ_PAGES` from the first page it did not accept. It never splices bytes across a
framing error.

### 3.6 Status codes

| Code | Name | Meaning |
|---:|---|---|
| `0x00` | `OK` | |
| `0x01` | `ERR_CRC` | request CRC mismatch; nothing was executed |
| `0x02` | `ERR_UNKNOWN_CMD` | |
| `0x03` | `ERR_BAD_ARGS` | wrong `arg_len`, value out of range, or disallowed READ_ID address |
| `0x04` | `ERR_RB_TIMEOUT` | R/B# did not go high within `rb_timeout_us` (or 10 ms for the pre-RESET wait) |
| `0x05` | `ERR_ABORTED` | stream stopped by `ABORT` or host disconnect |
| `0x06` | `ERR_BUSY` | request arrived while a stream was running |
| `0x07` | `ERR_TIMING_FLOOR` | `SET_TIMING` CUSTOM value below the firmware's Table 20 floor (rejected, not clamped) |

### 3.7 Opcode safety gate (firmware)

```c
/* nand_cmd.h — the ONLY opcodes this firmware can ever put on the bus (SPEC "Hard safety constraints"). */
typedef enum {
    NAND_CMD_READ_1      = 0x00, /* §3.1 Page Read, 1st cycle          */
    NAND_CMD_READ_2      = 0x30, /* §3.1 Page Read, 2nd cycle          */
    NAND_CMD_READ_STATUS = 0x70, /* §3.9                               */
    NAND_CMD_READ_ID     = 0x90, /* §3.16 / §3.18                      */
    NAND_CMD_READ_PARAM  = 0xEC, /* §3.19                              */
    NAND_CMD_RESET       = 0xFF, /* §3.12                              */
} nand_cmd_t;

#define NAND_CMD_IS_ALLOWED(op) ((op)==0x00||(op)==0x30||(op)==0x70||(op)==0x90||(op)==0xEC||(op)==0xFF)
/* Compile-time gate: every call site passes a constant, and it must be in the list. */
#define NAND_CMD(op) do { _Static_assert(NAND_CMD_IS_ALLOWED(op), "forbidden NAND opcode"); \
                          nand_bus_cmd_latch_((nand_cmd_t)(op)); } while (0)
```
- `nand_bus_cmd_latch_()` also runs a runtime `switch` allow-list and calls `panic()` **before** touching the bus if
  the opcode is anything else (defense in depth).
- There is **no data-input function** at all. No code path toggles WE# with CLE = ALE = 0.
- WP# has no GPIO, no define, no code.

### 3.8 BUS_TEST sequence (chip-safe)

The chip ignores everything while **CE# is high**, and its I/O pins are high-Z then (Table 3). So the whole test runs
with CE# high, except one CE# pulse during which there are no strobes and CLE = ALE = 0. It is harmless even with a
chip in the socket. One step = `step_us`.

| # | Action (each held 1 step, then released for 1 step) |
|---|---|
| 0 | idle: CE#=WE#=RE#=1, CLE=ALE=0, IO = output `00h`, held 10 steps (start marker) |
| 1 | CE# low → high |
| 2 | CLE high → low |
| 3 | ALE high → low |
| 4 | WE# low → high |
| 5 | RE# low → high |
| 6 | walking one: IO0 … IO7 one at a time |
| 7 | IO patterns `FFh`, `00h`, `55h`, `AAh` |
| 8 | IO → input, idle, 10 steps (end marker) |

Line order on the logic analyzer must show exactly this order. This will go into `docs/BUS_TEST.md`.
*Optional extension, not in the spec, your call:* return a 3-byte diagnostic payload with the data bus read back with
internal pull-ups and then pull-downs (CE# high) plus the R/B# level. Without a chip this finds stuck, open and shorted
IO lines before M2.

---

## 4. Timing

### 4.1 Model

One runtime struct, **values in clk_sys cycles** (125 MHz → 8 ns/cycle, reported in `PING`). Each value is a
**minimum**: `delay_cycles(n)` waits at least `n` cycles, and the real phase is longer by a few cycles of instruction
overhead. The host never has to meet a *maximum*, since tREA, tWB, tRHZ and tCHZ are guarantees the chip gives, so
running slower is always safe.

Sequencing per latch cycle (Fig. 12/13): set CLE/ALE/IO → `t_setup` → WE#↓ → `t_wp` → WE#↑ → `t_wh`.
Per data-out byte (Fig. 15): RE#↓ → `t_rea` → sample `gpio_get_all() & 0xFF` → RE#↑ → `t_reh`.

### 4.2 Proposed DEFAULT values

| Field | Covers (Table 20) | Datasheet | Default | ns | Margin |
|---|---|---|---:|---:|---|
| `t_cs` | CE#↓ → first strobe: tCS, tCR | 20 / 10 min | 6 | 48 | tCS measured to WE#↑: 48+24+32 = 104 ns → 5× |
| `t_setup` | CLE/ALE/IO valid → WE#↓: tCLS, tALS, tDS | 10 min each (to WE#↑) | 3 | 24 | to WE#↑ = 24+32 = 56 ns → 5.6× |
| `t_wp` | WE# low pulse: tWP | 12 min | 4 | 32 | 2.7× |
| `t_wh` | WE#↑ → change CLE/ALE/IO: tCLH, tALH, tDH, tCH; also WE# high time tWH | 5 / 10 min | 3 | 24 | holds 4.8×; WE# high = 24+24 = 48 ns → 4.8×; tWC = 24+32+24 = 80 ns vs 25 → 3.2× |
| `t_whr` | last WE#↑ → first RE#↓: tWHR, tAR, tCLR | 60 / 10 / 10 min | 15 | 120 | 2× |
| `t_rea` | RE#↓ → sample: tREA + 2-cycle input sync; tRP | tREA 20 max; tRP 12 min | 8 | 64 | effective sample at ≥ 48 ns after RE#↓ → 28 ns beyond tREA; tRP 5.3× |
| `t_reh` | RE#↑ → next RE#↓: tREH | 10 min | 3 | 24 | 2.4×; tRC ≥ 64+24 = 88 ns vs 25 → 3.5× |
| `t_rhw` | last RE#↑ → bus to output / WE#↓: tRHW, tRHZ | 100 min / 100 max | 25 | 200 | 2× |
| `t_wb` | last WE#↑ → first R/B# sample: tWB | 100 max | 25 | 200 | 2× |
| `t_rr` | R/B#↑ seen → first RE#↓: tRR | 20 min | 6 | 48 | 2.4× |
| `t_ceh` | CE#↑ → next CE#↓ / bus to output: tCSD, tCHZ | 10 min / 30 max | 8 | 64 | 2.1× over tCHZ |
| `rb_timeout_us` (u32) | tR, tRST | 25 µs / 5–500 µs max | 1000 µs | | 40× tR, 2× worst tRST |

Wire format of `timing_t`: 11 × `u16` in the order above (`t_cs` … `t_ceh`), then `u32 rb_timeout_us` = **26 bytes**.

Separately, the pre-RESET power-on wait is fixed at **10 ms** (§4.1 says 5 ms max).

**Floors.** The firmware checks every Table 20 constraint against the sum of the phases that make it up (e.g. tCS
against `t_cs + t_setup + t_wp`, tWC against `t_setup + t_wp + t_wh`), with ns converted to cycles and rounded **up**.
For `t_rea` it adds the 2-cycle input sync: 20 + 16 = 36 ns → 5 cycles. `SET_TIMING CUSTOM` below a floor returns
`ERR_TIMING_FLOOR`. So even a typo on the host cannot violate the datasheet.

**SLOW preset:** every `t_*` = **125 cycles (1 µs)**, and `rb_timeout_us` stays at 1000. At 24 MHz a logic analyzer
gets about 24 samples per phase.

### 4.3 Expected throughput (to be measured at M5, not a claim)

Per page at DEFAULT: about 2112 × (64+24+~40 ns loop overhead) ≈ 270 µs of data-out, plus ≤25 µs tR, plus <1 µs
cmd/addr, plus about 100–150 µs of table-driven CRC-32. That is ~0.45 ms/page, or about 4.7 MB/s from the NAND. USB
full-speed CDC (≈0.8–1 MB/s) is the bottleneck by 5×. **So the conservative bus timings cost no throughput**, and I see
no reason to tighten them before M5. Expected: 0.7–0.9 MB/s, so a full 276.8 MB pass takes about 5–7 minutes. PIO is
not needed for the 300 KB/s target.

---

## 5. Host tool behaviours worth confirming

- `dump` refuses to start unless `READ_ID` returns exactly `01 DA 90 95 44`. It stores ID, timing and firmware
  version in a sidecar `FILE.meta.json`. `--resume` continues from `filesize // 2112` (a partial last page is
  truncated) and refuses if the sidecar parameters differ. It requests pages in chunks of 1024 (≈3 s) and re-checks
  `READ_ID` between chunks, which catches the chip losing contact in the clamshell socket.
- A page that still fails after the retry limit (default 5) **aborts the dump**. It is resumable, and nothing is ever
  zero-filled. Such failures are systemic (wiring, socket), not data-dependent.
- `status` decodes SR bits and **warns loudly if bit 7 = 1** (WP# not grounded).
- `id` also decodes bytes 3–5 (Tables 16, 3.2, 3.3): SLC, 2 KB page, 128 KB block, 2 planes × 1 Gb, ×8, 25 ns.

---

## 6. Decisions (resolved 2026-09-30: "approved")

1. Protocol byte format, including `seq`, the end frame (`cmd | 0x80`), `ABORT`, `ERR_BUSY`, `ERR_TIMING_FLOOR`,
   and READ_ID `20h`: **approved**.
2. DEFAULT timing table and SLOW = 1 µs/phase: **approved**.
3. `READ_STATUS` always prefixes the dummy `00h`: **approved**.
4. READ_PAGES continues past an R/B# timeout and the host retries afterwards: **approved**.
5. BUS_TEST diagnostic readback payload: **included**. The blanket approval covered the proposal with this
   extension listed. It is additive and can be dropped on request.
6. VID:PID `2E8A:000A`: **approved**.

Later change: the repo was renamed `pico_nand_dumper` → `pico_nand_tool` because other capabilities may be added
later. The SPEC's read-only constraints are unchanged.
