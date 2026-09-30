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
- 2026-09-30: M0 code is written. Firmware: USB CDC + PING/SET_TIMING/ABORT, with NAND lines parked idle. Host:
  ping/timing, fake device, and tests. The build and all tests pass. **Waiting for the user's hardware output for M0**
  (enumeration + `nandtool ping`). M0 has NOT been confirmed on hardware.

**Why:** SPEC forbids claiming a hardware milestone without the user's pasted output.
**How to apply:** Update this file whenever the user confirms a milestone. Implement the next milestone only after
the previous one is confirmed. See [[datasheet-findings]].
