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

**Why:** SPEC forbids claiming a hardware milestone without the user's pasted output.
**How to apply:** Update this file whenever the user confirms a milestone. Implement the next milestone only after
the previous one is confirmed. See [[datasheet-findings]].
