# Pico NAND Tool — Project Spec

## Goal

Build a raw NAND dumper and writer using a Raspberry Pi Pico (RP2040) to extract a full raw image (data + OOB/spare) from a desoldered **Spansion S34ML02G100BHI00** (2 Gbit SLC NAND, x8, 3.3 V, BGA63) mounted in a BGA63 clamshell socket, and to write a raw image back to the same chip or onto another one (write mode, added 2026-10-05; design: `docs/WRITE_PROPOSAL.md`).

Deliverables:

1. Pico firmware (C, Pico SDK, TinyUSB) that bit-bangs the NAND bus and streams pages over USB.
2. A Python host tool that drives the firmware, verifies transfers, dumps the chip with retries and multi-pass verification, and writes the image.
3. Post-processing helpers: data/OOB split and factory bad-block scan.
4. Write mode: block erase and full-page program, armed at runtime, with readback verification (see "Write mode").

The datasheet is in `docs/` (S34ML01G1/S34ML02G1/S34ML04G1, Rev *W). **Treat it as the source of truth** for all commands and timings; cite section/table numbers in code comments.

## Hard safety constraints (non-negotiable)

- The only NAND commands the firmware may ever issue are: `FFh` (Reset), `90h` (Read ID), `ECh` (Read Parameter Page), `00h`/`30h` (Page Read), `70h` (Read Status), and, **in write mode only**, `80h`/`10h` (Page Program, full page) and `60h`/`D0h` (Block Erase). Enforce this in code: commands are issued through enums with compile-time/static checks, read and write opcodes through separate gates, plus a runtime check before anything reaches the bus. Every other opcode stays forbidden, including `85h` (random data input), copy-back, cache, multiplane, `78h` and OTP.
- Write mode is off at boot. The host arms it at runtime for an explicit block range and a short idle timeout; it disarms on any error, DTR change, reset or timeout. Program/erase outside the armed range never reaches the bus. The data-input cycle (WE# with CLE = ALE = 0) exists only inside the Page Program sequence.
- WP# is wired to GP13 with a 10k external pull-down to GND (see pin map). The firmware drives it **low** as the first thing it does at boot. It drives WP# high **only** for the duration of a single armed program or erase operation (≥ tWW before the setup command), and low again right after, or at once on timeout or error (which aborts the operation, datasheet §4.3). It never pulls it up. A fault, panic or hang must leave WP# low.
- The firmware never erases a block whose factory bad-block marker (§9.2) is not `FFh`, unless the host explicitly overrides it for that one request.
- The dump is the only copy of the data. The host tool must never overwrite an existing output file without an explicit flag.
- Before erasing, the host tool warns loudly and requires typed confirmation (or an explicit `--yes`), and checks a `--backup` against the chip when one is given. It saves the target's bad-block markers before the first erase, verifies every written block by readback, and stops at the first failure. It never marks, remaps or skips a failing block silently.

## Hardware

### Target chip facts (from datasheet)

| Item | Value |
|---|---|
| READ ID (90h, addr 00h) | `01 DA 90 95 44` |
| Page | 2048 data + 64 spare = 2112 bytes |
| Block | 64 pages |
| Blocks | 2048 (2 planes × 1024) |
| Total pages | 131072 → raw image 276,824,064 bytes |
| Address cycles | 5: 2 column + 3 row (param page byte 101 = 23h) |
| Row address | page index 0..131071 (block = page >> 6) |
| tR (array → register) | ≤ 25 µs |
| tRST | ≤ 5 µs from ready state |
| Max factory bad blocks | 40 (param page bytes 103–104 = 28h, 00h) |

### AC timing minimums (Table 20) — must be honored, all ns

tCLS 10, tCLH 5, tALS 10, tALH 5, tCS 20, tCH 5, tDS 10, tDH 5, tWP 12, tWH 10, tWC 25, tADL 70, tWB ≤100 (max), tWHR 60, tRR 20, tRP 12, tREH 10, tRC 25, tREA ≤20 (max), tRHW 100, tAR 10, tCLR 10.

RP2040 notes:

- At 125 MHz one cycle is 8 ns. GPIO input passes through a 2-cycle synchronizer (~16 ns added latency), so the RE#-low→sample delay must cover tREA + sync + margin.
- Put all delays in a single runtime-adjustable timing struct (cycle counts) with conservative defaults (e.g. ~64 ns strobe-to-sample, ≥2× every minimum). Add a `SLOW` mode (≈1 µs per phase) selectable at runtime so a 24 MHz logic analyzer can resolve every edge.

### Pin map (Pico → NAND BGA63 ball)

| Pico | Signal | Ball | Notes |
|---|---|---|---|
| GP0–GP7 | I/O0–I/O7 | H4, J4, K4, K5, K6, J7, K7, J8 | Contiguous so a byte read is `gpio_get_all() & 0xFF` |
| GP8 | CLE | D5 | |
| GP9 | ALE | C4 | |
| GP10 | WE# | C7 | 10k external pull-up to 3V3 |
| GP11 | RE# | D4 | 10k external pull-up to 3V3 |
| GP12 | CE# | C6 | 10k external pull-up to 3V3 |
| GP13 | WP# | C3 | **10k external pull-down to GND**. Firmware drives it low at boot; high only during one armed program/erase |
| GP14 | R/B# | C8 | Input; open-drain, 10k external pull-up to 3V3 |
| 3V3(OUT) pin 36 | VCC | D3, G4, H8, J6 | 100 nF + 10 µF at socket |
| GND | VSS | C5, F7, K3, K8 | |

Keep pin assignments in one header so they can be changed. Note RP2040 pads reset with internal pull-downs; the external pull-ups keep CE#/WE#/RE# deasserted before firmware init. On init, drive CE#/WE#/RE# high *before* enabling them as outputs.

## Firmware requirements

- C, Pico SDK, CMake. Build produces a `.uf2`.
- Bus driver uses SIO mask operations (`gpio_set_mask`, `gpio_clr_mask`, `gpio_put_masked`, `gpio_set_dir_*_masked`), not per-pin loops. Data bus is output only during command/address cycles; switch to input before the first RE# fall (respect tWHR) and back to output only after tRHW.
- R/B# polling: wait ≥ tWB after the last WE# rising edge before sampling R/B#; poll with a timeout (e.g. 1 ms); report timeouts as an error status, never hang.
- **USB transport must be binary-safe.** Do NOT use `stdio_usb`/`printf` for data (it can translate `\n`→`\r\n`). Use TinyUSB CDC directly (`tud_cdc_write`/`tud_cdc_read`) with proper flow control, or a vendor-class interface if you judge it better (justify the choice). No debug prints on the data channel.
- Page reads may be streamed (host requests a range, device sends one framed response per page) so USB stays busy; the device must not overrun its buffers.

### Commands (host → device)

Design a compact binary framing with a magic byte, command, args, and CRC. At minimum:

| Command | Action | Response payload |
|---|---|---|
| PING | Liveness + firmware version | version string |
| BUS_TEST | Toggle each control line and each data line in a fixed, documented sequence (for logic-analyzer verification without a chip) | none |
| SET_TIMING | Set timing struct / SLOW mode | echo of active timings |
| RESET | FFh, wait ready | status |
| READ_ID | 90h + addr 00h, read 5 bytes | 5 bytes |
| READ_STATUS | 70h, read 1 byte | 1 byte |
| READ_PARAM | FFh (Reset) first — required by datasheet §3.19 note for 41 nm 2 Gb parts — then ECh + addr 00h, wait ready, read 768 bytes (3 redundant copies) | 768 bytes |
| READ_PAGES(start, count) | For each page: 00h, 5 addr bytes (col 0,0; row LSB first), 30h, wait ready, wait tRR, read 2112 bytes | one frame per page: page index + 2112 bytes |

Every device → host frame carries: magic, command, status code, page index (if applicable), payload length, payload, and a CRC32 over header+payload.

## Host tool requirements (Python 3, pyserial)

Subcommands:

- `ping`, `bus-test`, `id`, `status`
- `param`: read all 3 copies, verify signature `ONFI` and the ONFI CRC-16 (poly 0x8005, init 0x4F4E, MSB-first, no final XOR, over bytes 0–253, stored LE in 254–255) for each copy; decode and print geometry. **Expected for this chip: bytes 254–255 = `3B C5`.** Port the algorithm from Linux `drivers/mtd/nand/raw/nand_onfi.c` (`onfi_crc16`).
- `read --page N [--repeat K]`: read one page K times, report whether all reads are identical and where they differ.
- `dump --out FILE [--start N --count M]`:
  - Writes raw 2112-byte pages in order.
  - Retries any page whose frame CRC fails (transport error) up to a limit.
  - Resumable: can continue an interrupted dump from the last completed page.
  - Progress, ETA, throughput.
  - Refuses to overwrite an existing file unless `--force`.
- `compare A B`: compare two dumps page by page; for differing pages, list byte/bit differences.
- `reconcile A B --out FINAL`: for pages that differ between passes, re-read each K times (default 5) from the device and take a per-bit majority vote; write the final image and a JSON report of every corrected bit (page, offset, bit, vote counts). Pages that never stabilize are flagged, not silently "fixed".
- `split IMAGE`: produce `data.bin` (2048 B/page) and `oob.bin` (64 B/page).
- `badblocks IMAGE`: factory bad-block scan per datasheet §9.2 — a block is bad if the first spare byte of its 1st, 2nd or last page is not FFh. Output the list.

Write mode (protocol v2, `docs/PROTOCOL.md`):

- `erase --block B [--count N]`: erase blocks (typed confirmation), verify all `FFh`.
- `program --page N --in FILE`: program one 2112-byte page into an erased page, verify by readback.
- `write IMAGE`: write a raw image. Default **only-changed**: blocks identical on the chip are skipped, so a patched image erases only the patched blocks. Block map `1:1` (default; refuses before any erase if a target bad block would receive data) or `skip-bad`. Pages that are all `FFh` are not programmed. Every written block is read back and compared. ECC/OOB is written raw, never recomputed.
- `badblocks --device`: scan the chip's bad-block markers live; `badblocks IMAGE --blank`: list all-`FFh` blocks.

Also provide a **fake device** (Python class simulating the firmware protocol over a synthetic NAND image, with injectable transport errors and bitflips; in write mode it models erase, AND-only programming, NOP counts, arming and program/erase failures) so all host logic is unit-tested without hardware. Use pytest.

## Milestones and acceptance criteria

Work strictly in this order. **At each hardware milestone, stop and tell me exactly what to run and what output you expect; I will run it and paste results.** Never claim a hardware milestone passed without my pasted output.

- **M0 — Toolchain & USB:** firmware builds; Pico enumerates; `ping` returns the version. Host unit tests pass against the fake device.
- **M1 — Bus test (no chip):** `bus-test` in SLOW mode; I verify line order and levels with a logic analyzer.
- **M2 — Chip alive:** `status` after RESET shows ready; `id` returns exactly `01 DA 90 95 44`, identical over 1000 consecutive reads.
- **M3 — Bus integrity:** `param` passes signature + CRC on all 3 copies with CRC bytes `3B C5`; decoded geometry matches the table above. This is the primary end-to-end wiring check — any stuck, swapped or flaky data line fails it.
- **M4 — Page reads:** page 0 read 100× identical; block 0 and a few random blocks read cleanly at default timing.
- **M5 — Full dump:** two complete passes, `compare`, then `reconcile`. Target throughput ≥ 300 KB/s, but correctness first — optimize (e.g. PIO) only after M5 passes.
- **M6 — Post-processing:** `split` and `badblocks` on the final image.

Write mode (`docs/WRITE_PROPOSAL.md` §8), in this order:

- **W1 — Code, no hardware:** protocol v2, firmware, host, fake device, gates; CI green.
- **W2 — Regression + interlocks:** M2–M4 checks unchanged; erase while disarmed and outside the armed range → `ERR_NOT_ARMED`; `badblocks --device` matches the dump.
- **W3 — First erase** of a block that is all `FFh` in the final dump: SR pass, readback all `FFh`, rest of the chip unchanged.
- **W4 — First program:** test patterns into that block, readback ×100 identical and equal; erase again → all `FFh`.
- **W5 — Same-chip restore** of one block with real data from the final dump: readback equals the dump.
- **W6 — Full chip:** `write` the final dump (only-changed, then `--all`), then a fresh dump has the original SHA-256.
- **W7 — Clone** onto a second, blank chip: dump equals the image except bad blocks.

## Out of scope (for now)

Partial-page programming and random data input (`85h`), copy-back, cache and multiplane operations (only full-page program and single-block erase are in scope); automatic grown-bad-block marking or remapping; ECC computation, correction or OOB layout decoding (depends on the original SoC's controller — later phase); filesystem extraction (binwalk/ubi_reader happens after this project).

## References

- Datasheet in `docs/` — authoritative.
- Linux `drivers/mtd/nand/raw/nand_base.c`, `nand_onfi.c` — command sequences, ONFI CRC.
- https://github.com/r00t1024/rpi-tsop48-nand (`rpi-raw-nand-v3.c`) — simple bit-bang page-read reference (Raspberry Pi SBC, not Pico; its permanently-high WP# and unguarded write/erase code are exactly what we must not replicate).
- https://github.com/wipeseals/nandio.pio — RP2040 PIO NAND interface (possible later optimization).
- https://github.com/bbogush/nand_programmer (NANDO) — firmware/host protocol and bad-block handling design reference.

## Working style

- Plan first: propose repo layout, protocol byte format, and timing defaults for my review before writing the bulk of the code.
- Keep firmware small and readable; comment every NAND sequence with the datasheet figure/table it implements.
- If something in this spec conflicts with the datasheet, stop and tell me.
