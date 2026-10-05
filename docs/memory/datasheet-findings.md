---
name: datasheet-findings
description: Non-obvious datasheet facts verified 2026-09-30 — SR=60h after reset (WP# low), dummy 00h before 70h after Read ID, 5 ms power-on busy, golden param page CRC
metadata:
  type: project
---

Verified against datasheet Rev \*W on 2026-09-30. SPEC.md has no conflicts with the datasheet.

- The status register after RESET is **`60h`** because WP# is low (§3.12). `E0h` means WP# is not low: stop and
  check the hardware. (Since 2026-10-04 WP# goes to GP13 with a 10k pull-down, and the firmware drives it low.)
- Read ID → Read Status needs a dummy `00h` first (§3.16 note).
- Power-on: the chip is busy for ≤5 ms and accepts only `70h` (§4.1). Wait for R/B# (10 ms timeout) before `FFh`.
- tRST is 5/10/500 µs from ready/read/program busy (Table 20).
- Balls D3, G4 (VCC) and F7 (VSS) "might not be bonded" (Fig. 3 note 1). The guaranteed ones are H8, J6 (VCC) and
  C5, K3, K8 (VSS).
- Rebuilding the parameter page from Table 3.4 (S34ML02G100 ×8) gives ONFI CRC-16 `0xC53B` → bytes `3B C5`. This
  reference page is the fake-device/test fixture. Fields: 'ONFI', rev 02 00, features 1C 00, opt cmds 1B 00,
  "SPANSION    ", "S34ML02G1" padded to 20, JEDEC 01, 2048/64/512/16 B, 64 pages/blk, 2048 blk/LUN, 1 LUN,
  addr 23h, 1 bit/cell, bad 28 00, endurance 01 05, guaranteed 01, 01 03, NOP 04, 111=00, ECC 01, interleave 01,
  attrs 04, pin cap 0A, timing modes 1F 00 / 1F 00, tPROG BC 02, tBERS 10 27, tR 19 00, tCCS 64 00, rest 0.

Write mode (checked 2026-10-05):

- tPROG typ 200 / max 700 µs, tBERS typ 3.5 / max 10 ms, NOP = 4 partial programs per page (Table 23). tADL 70 ns,
  tWW 100 ns (Table 20). Pages may be programmed in any order within a block (§3.2): no sequential-page rule.
- WP# must be at level ≥ tWW before WE#↑ of `80h`/`60h`; pulling WP# low while busy aborts the program/erase like
  `FFh`, and the page/block is then undefined until a clean erase (§4.3, §3.2, §3.5).
- After program/erase, SR bit 7 read while WP# is still high proves WP# really rose (bit 7 = 0 → chip ignored the
  command). Bit 0 = fail, bit 6 = ready (Table 13).
- Read the bad-block markers before any erase: an erase can destroy them (§9.2).

**Why:** these facts are easy to get wrong, and they change M2/M3 expectations.
**How to apply:** use them for firmware sequences, host `status` decoding, and the tests. See
[[milestone-status]].
