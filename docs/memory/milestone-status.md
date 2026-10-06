---
name: milestone-status
description: Where the project is in the SPEC milestones M0-M6 and which hardware results the user has confirmed
metadata:
  type: project
---

- 2026-09-30: The user approved `docs/PROPOSAL.md` as written, including BUS_TEST readback, READ_STATUS dummy 00h,
  continue-on-R/B#-timeout, and VID:PID 2E8A:000A. The protocol is now canonical in `docs/PROTOCOL.md`.
- 2026-09-30: The repo was renamed `pico_nand_dumper` → `pico_nand_tool` (GitHub KamranAghlami/pico_nand_tool),
  because "we might add writing or other capabilities later". SPEC.md is still read-only and the hard safety
  constraints are unchanged. The local checkout directory may still be called `pico_nand_dumper`.
- 2026-09-30: The user chose to KEEP the PROTOCOL.md stream semantics (immediate ERR_BUSY between page frames;
  ABORT gets its own OK after the end frame). Recorded in PROPOSAL §6 item 7.
- 2026-09-30: M0 code is written. Code-review fixes (15 findings, incl. opcode-gate hardening) were applied the same
  day. Firmware: USB CDC + PING/SET_TIMING/ABORT, with NAND lines parked idle. Host: ping/timing, fake device, and
  tests. The build and all tests pass.
- 2026-10-01: **M0 PASSED (user confirmed).** On hardware: enumerates as 2E8A:000A (/dev/ttyACM0 via usbipd on
  WSL2); `nandtool ping` → `pico-nand-tool 0.1.0 (2c9908b)`, protocol v1, clk_sys 125 MHz; `timing --slow/--default`
  round-trip OK; 50/50 consecutive pings OK. Next: M1 (bus-test in SLOW mode, no chip; user checks with a logic
  analyzer). M1 is NOT yet implemented.
- 2026-10-01: The LED status/activity feature (7ecb82b) was reverted at the user's request (a749da2). Its panic
  handler went with it; plan to bring back only "park the NAND bus on panic" at M2.
- 2026-10-01: The user verified on macOS: the README macOS build/flash/install steps, and the 1200-baud BOOTSEL
  reboot (`stty -f /dev/cu.usbmodem* 1200`). The user works on both macOS and WSL2 (usbipd).
- 2026-10-04: Firmware 39df358 (WP# on GP13) built on WSL2 and re-ran the M0 checks on hardware (ping, timing,
  50/50 pings). The WSL toolchain lives at ~/pico-sdk. That machine's SDK-built picotool has no USB support, so flash
  by mounting the RPI-RP2 drive (/dev/disk/by-label/RPI-RP2) and copying the .uf2.
- 2026-10-04: **M1 skipped by the user**: no logic analyzer. The user soldered the NAND and asked to go straight
  to "does the chip respond". BUS_TEST is still unimplemented.
- 2026-10-04: M2 code written (RESET / READ_ID / READ_STATUS, `nandtool id`, `nandtool status`). On hardware, run in
  the session: `status --reset` → 60h (WP# low, ready); `id --onfi` → 01 DA 90 95 44 + "ONFI"; `id --repeat 1000`
  → 1000/1000 identical. **Open issue:** RESET always reports R/B# never seen low (busy_ns = 0), even right after
  Read ID. Either tRST from idle is shorter than tWB + poll latency (~0.3 µs), or R/B# (ball C8 → GP14) is open and
  the 10k pull-up holds it high. M3 (READ_PARAM, tR ≈ 25 µs) should tell which.
- 2026-10-04: **M2 APPROVED by the user** on that output (R/B# question still open), committed; M3 next.
- 2026-10-04: M3 code written (READ_PARAM, `nandtool param`, host/nand_tool/onfi.py, Table 3.4 reference page in
  host/tests/golden_param.py; CRC 0xC53B confirmed). On hardware, run in the session: `param` → all 3 copies
  signature + CRC `3B C5` OK, identical, geometry matches the SPEC; 100/100 reads identical and byte-identical to
  the reference page. That also argues R/B# works: copy 0 is read first, right after tR, and would fail if the
  wait ended early. Not yet airtight; M4 page reads (tR ≤ 25 µs) are the next check.
- 2026-10-04: **M3 APPROVED by the user**; M2 + M3 committed and pushed. M4 next.
- 2026-10-04: M4 code written (nand_read_page, READ_PAGES stream with ABORT/ERR_BUSY/DTR handling, shared RX
  buffer so a nested poll keeps arrival order; host Client.read_pages with the recovery rule; `nandtool read`).
  On hardware, run in the session: page 0 x100 identical; blocks 0, 1, 777, 1024, 1531, 2047 x3 identical, no R/B#
  timeouts; pages 64000..66047 x2 identical at ~950 KiB/s (SPEC target 300 KB/s). Early stop, mid-stream ERR_BUSY
  and an ABORT in the same packet as READ_PAGES all behave per PROTOCOL.md. Stable full-speed page data also settles
  the R/B# question: it works; a reset of an idle chip is just faster than the first sample.
- 2026-10-04: **M4 APPROVED by the user**; committed and pushed. M5 next.
- 2026-10-04: M5 code written (dump.py, compare.py, reconcile.py; `nandtool dump/compare/reconcile`). On hardware,
  run in the session: two full passes `dumps/pass1.bin`, `dumps/pass2.bin` (131072 pages each, 5:20 and 5:14,
  844/861 KiB/s, 0 retries, 0 restarts), both with the same SHA-256; `compare` identical; `reconcile` → 0 pages
  differ, `dumps/final.bin` (same SHA). Ctrl-C + `--resume` verified on hardware. The dumps live only in the
  gitignored `dumps/` dir on the WSL machine.
- 2026-10-04: **M5 APPROVED by the user**; committed and pushed. M6 next.
- 2026-10-04: M6 code written (postproc.py; `nandtool split`, `nandtool badblocks`). On `dumps/final.bin`, run in
  the session: badblocks → 0 bad blocks of 2048 (spare byte 0 is FFh on all 131072 pages); split → `dumps/data.bin`
  (256 MiB) + `dumps/oob.bin` (8 MiB), re-interleaving gives the original SHA-256.
- 2026-10-04: **M6 APPROVED by the user**; committed and pushed. The user **dropped M1** for good ("we don't need
  m1, it works"). BUS_TEST stays unimplemented (ERR_UNKNOWN_CMD). SPEC.md still lists M1; it is user-owned, so it
  was not edited. All milestones are closed.
- 2026-10-05: **Write mode planning started.** User decisions: both use cases (same-chip modified image and
  cloning), ONE firmware with runtime arm (no separate RW build), minimal ops (erase + full-page program; partial
  page only if partial reads ever exist), backup = warn only. Draft: `docs/WRITE_PROPOSAL.md` (milestones W0-W7).
  Not approved; no write code until the user changes SPEC.md (W0).
- 2026-10-05: **Write proposal APPROVED** ("approved, feel free to update spec as needed"). W0: SPEC.md, docs/CLAUDE.md
  hard rules and PROTOCOL.md (v2) updated by Claude on that authority. W1 code written (firmware 0.2.0 / host
  0.2.0, protocol v2): NAND_WCMD gate + write window + sequence checks + data-in only in nand_write.c, WP# raised
  only in nand_bus_write_window_open, ARM_WRITE/DISARM/ERASE_BLOCK/PROGRAM_PAGE, watchdog, HardFault parks the bus;
  host erase/program/write/interlock-test, badblocks --device/--blank. make check, SDK build and 188 host tests
  pass. Nothing run on hardware yet. Next: W2 (flash 0.2.0, read regressions, `interlock-test` on a block that is
  blank in final.bin, `badblocks --device`, `write final.bin --dry-run` should find nothing to write).
- 2026-10-05: **W2 APPROVED by the user** (WSL machine, firmware 0.2.0 (040af54), protocol v2). Before flashing, the old
  firmware stopped answering (`vhci_hcd ... urb->status -104`) and the 1200-baud reboot did nothing. The cause was a
  bad USB cable, not firmware or usbip; the user swapped it and put the Pico in BOOTSEL by hand. Results: `status --reset` 60h; `id --repeat 1000 --onfi` 1000/1000 + ONFI; `param` 3 copies
  CRC `3B C5`, PASS; page 0 x100 PASS; `badblocks --device` 0 bad (same as final.bin); `interlock-test --block 80`
  8x ERR_NOT_ARMED + 1x ERR_BAD_ARGS, SR 60h, block still blank, PASS; `write final.bin --dry-run` (4:52) 0 bad
  target blocks, 2048 identical, nothing to write.
  Block 80 is the W3 sacrificial block. W3 next.
- 2026-10-05: **W3 APPROVED by the user.** First real erase, block 80 (blank in final.bin): `erase --block 80` → SR
  E0h (bit 7 = 1, so WP# rose), busy 1144 us (below the 3.5 ms typ; tBERS has no minimum, and the block was already
  blank), verified all FFh; `status` 60h afterwards; `read --block 80 --repeat 10` 64/64 FFh, stable;
  `write final.bin --dry-run` 2048/2048 identical. `nandtool erase` now prints SR + busy (61fd1d0). In auto mode the
  permission classifier refuses `nandtool erase` even with an allow rule; the user switched to the default mode.
- 2026-10-05: **W4 PASSED** (run in the session; the user had said "when passed, commit & push, then onto the next
  milestone"). Block 80, 64 pattern pages x 2112 B incl. OOB: 00, FF, 55, AA, 55/AA, AA/55, walking 1/0 (8 each),
  incr, decr, 1<<(o%8), seeded random (seed 0x5733 + page index); spare byte 0 kept FFh on pages 0, 1, 63 so the
  block does not look factory-bad (§9.2). `program` per page: SR E0h, busy 201-203 us (typ 200), readback identical.
  `read --block 80 --repeat 100` stable; dump of the block = pattern (sha256 4f311790…5159). Erase back: SR E0h,
  busy 1146 us, so ~1.1 ms is this chip's tBERS whether the block is blank or not (max 10 ms). Then 64/64 FFh x10,
  and `write final.bin --dry-run` 2048/2048 identical.
- 2026-10-05: **W5 PASSED** (run in the session under the same go-ahead). Block 1024 (64/64 pages with data + OOB):
  `write final.bin --first-block 1024 --count 1 --all --backup final.bin` → written, verified; dump of the block =
  final.bin's block. Then a modified copy (18 bytes changed on pages 10 and 40, one in OOB)
  via `write mod1024.bin --start 65536` (only-changed found the block) → dump = mod image, compare vs
  the original shows exactly those 18 bytes. Restored with `write final.bin --first-block 1024 --count 1` → dump =
  the original block; `write final.bin --dry-run` 2048/2048 identical. `write` leaves `<image>.write.json` (complete: true)
  next to the image, e.g. dumps/final.bin.write.json.
- 2026-10-05: **W6 PASSED** (run in the session under the same go-ahead). `write final.bin --backup final.bin` →
  nothing to write (4:54). `write final.bin --all --backup final.bin` → 2048 blocks written and
  verified, 0 failures/retries, 22:42. Fresh `dump --out dumps/w6.bin` (5:24, 834 KiB/s, 0 retries) → the same
  SHA-256, `compare` identical to final.bin. W7 (clone onto a second, blank chip) waits for that chip.
  Write speed: ~715 ms per full block = 64 x ~7.6 ms to send the 2112 B PROGRAM_PAGE request + ~45 ms tPROG/replies
  + ~170 ms verify read + ~13 ms erase/arm. Host→device runs at ~290 KB/s (linear, ~3.4 us/byte, measured with
  refused requests) vs ~770 KB/s device→host, on WSL2/usbip. Not yet known whether usbip or the RP2040 OUT path
  is the limit; a native-USB (macOS) run would tell.
- 2026-10-05: **User decision: readback is opt-in.** `erase`, `program` and `write` read back only with `--verify`
  (they print "verify : off" otherwise; the SR status check after every op stays). The user chose this over an
  opt-out `--no-verify` after hearing that verify is ~24% of write time and the only end-to-end check of the
  bit-banged data path. SPEC.md, the docs/CLAUDE.md hard rule, PROTOCOL.md, README and a WRITE_PROPOSAL note were
  updated. Checked on hardware on block 80 (erase with/without --verify; pattern write without, `--dry-run` matched;
  restored blank with --verify).
- 2026-10-05: **Write speed, root cause + fix.** PC→Pico tops out at ~440 KiB/s even for plain filler bytes:
  TinyUSB's RP2040 driver keeps device OUT endpoints single-buffered (pico-sdk 2.3.1
  lib/tinyusb/src/portable/raspberrypi/rp2040/rp2040_usb.c:161, "skip double buffered for OUT endpoint in Device
  mode"), while IN is double-buffered (~830 KiB/s reads). On top, stop-and-wait cost ~3 ms per page. Fix (host only,
  no firmware change): Client.program_pages keeps 2 PROGRAM_PAGE requests in flight (PROTOCOL.md "Pipelining");
  depth 3-4 gains nothing. Measured on blocks 1024-1031: 8.5 → 5.8 ms per page, 248 → 363 kB/s programmed;
  readback = final.bin. Going further would mean patching TinyUSB for double-buffered OUT (not done).

**Why:** SPEC forbids claiming a hardware milestone without the user's pasted output.
**How to apply:** Update this file whenever the user confirms a milestone. Implement the next milestone only after
the previous one is confirmed. See [[datasheet-findings]].
