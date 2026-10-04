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

**Why:** SPEC forbids claiming a hardware milestone without the user's pasted output.
**How to apply:** Update this file whenever the user confirms a milestone. Implement the next milestone only after
the previous one is confirmed. See [[datasheet-findings]].
