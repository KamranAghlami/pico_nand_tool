# Write Mode Proposal (approved)

Status: **APPROVED by the user on 2026-10-05** ("approved, feel free to update spec as needed"). `SPEC.md` was
updated the same day from §9. This is now a design record; the canonical wire protocol is `docs/PROTOCOL.md`.
Implementation details that differ from the text below (W1, 2026-10-05):
- WP# is raised and lowered in `nand_bus.c` (`nand_bus_write_window_open/close`), called only from `nand_write.c`,
  because `MASK_WP` stays confined to `pins.h` and `nand_bus.c`.
- The host arms one block at a time (range = that block), not the whole job's range.
- The target's markers and the block in progress live in one sidecar, `<IMAGE>.write.json`, not in separate
  `.target-bbt.json` / `.meta.json` files. Resuming is simply running `write` again: only-changed mode skips the
  finished blocks, and the block in progress is always re-written. `--fresh` ignores an unfinished earlier run (for
  a different chip).
- `write` flags: `--first-block` (not `--start-block`), plus `--dry-run` and `--fresh`. W2 uses a new
  `nandtool interlock-test --block B` on a blank block.
- After W6 (2026-10-05) the user made the readback opt-in: `erase`, `program` and `write` read back only with
  `--verify`, and say so when they don't. The chip's status is still checked after every operation. SPEC.md was
  changed accordingly; the "verifies every programmed block" text in §9 below is superseded.

Datasheet references are to doc 002-00676 Rev \*W (`docs/datasheet.pdf`), as in `PROPOSAL.md`.

Decisions already made (2026-10-05):

| Question | Answer |
|---|---|
| Use case | **Both**: write a modified image back to the same chip, and clone an image onto a different chip. |
| Separation | **One firmware, runtime arm.** No separate read-write build. |
| Operations | **Minimal**: Block Erase and full-page Page Program. No partial page, because reads are always full-page. |
| Backup before erase | **Warn only**: a loud warning plus typed confirmation. A `--backup` is checked if given, but not required. |

---

## 1. Datasheet facts the write path depends on

| Fact | Source | Consequence |
|---|---|---|
| Page Program: `80h`, 5 address cycles, up to 2112 data cycles, `10h`. tADL ≥ 70 ns from the last address WE#↑ to the first data WE#↑ | §3.2, Fig. 19, Table 20 | New bus primitive: data-input cycle (WE# with CLE = ALE = 0). New timing field `t_adl`. |
| tPROG typ 200 µs, **max 700 µs** | Table 23 | Program R/B# timeout 2 ms (≈ 3 × max). |
| Block Erase: `60h`, 3 row cycles (page bits ignored), `D0h` | §3.5, Fig. 24 | Erase takes a block number; firmware builds the row address. |
| tBERS typ 3.5 ms, **max 10 ms** | Table 23 | Erase R/B# timeout 20 ms. |
| NOP (partial programs per page between erases) = 4 | Table 23 | We use 1: every page is programmed at most once per erase, whole page. |
| "Pages may be programmed in any order within a block" | §3.2 | No sequential-page constraint (we program in ascending order anyway). |
| Only `70h`/`78h` and `FFh` are valid while programming or erasing | §3.2, §3.5 | We wait on R/B#, then read status once. |
| SR bit 0 = pass (0) / fail (1) for program and erase; bit 6 = ready; bit 7 = 0 when protected | Table 13 | Pass/fail check after every operation. **SR bit 7 also proves WP# really went high**: a stuck-low WP# shows up before anything is erased. |
| WP# must be at its level **tWW ≥ 100 ns** before WE#↑ of the setup command (`80h`/`60h`) | §4.3, Fig. 47/48, Table 20 | New timing field `t_ww`. |
| Pulling WP# low during busy **aborts** program/erase (like `FFh`); the page/block content is then invalid | §4.3 | Safe failure: on timeout, panic or any error the firmware drops WP#. The block must be re-erased before use (§3.2/§3.5), which the host's resume handles. |
| tRST up to 10 µs (program busy), 500 µs (erase busy) | Table 20 | Recovery reset after an abort uses the erase timeout. |
| Interrupted program/erase: the page/block must not be trusted until a clean erase | §3.2, §3.5 | `write` resume always re-erases the block it was in. |
| **Read bad-block info before any erase; erasing can destroy it** | §9.2 | The host scans target markers before the first erase and saves them. The firmware refuses to erase a block whose marker is not `FFh` (§3.4). |
| Program/erase fail = new bad block; other pages of the block are unaffected | §9.1 | Stop, report the block, do not continue silently. |
| 100 000 P/E cycles typ (with ECC); blocks 0 and 1 guaranteed for 1 000 | Features | One full-chip rewrite is negligible wear. Only-changed-blocks mode keeps it lower still. |

No conflict between SPEC and datasheet found for this scope.

## 2. What stays forbidden

Only `80h`, `10h`, `60h`, `D0h` are added. Still forbidden, at compile time and at run time: `85h` (random data
input), `35h`/`85h` copy-back, `11h`/`81h` multiplane, `15h` cache program, `31h`/`3Fh` cache read, `78h`, OTP and
anything else. The data-input cycle exists in exactly one place, the program sequence.

## 3. Firmware safety design (one firmware, runtime arm)

Defense in depth, so no single bug or stray host frame can erase anything:

1. **Two opcode lists.** The `NAND_CMD()` read list is unchanged. A new `NAND_WCMD()` macro statically accepts only
   `80h 10h 60h D0h`. `make check` proves `85h`, `0x80` through `NAND_CMD()`, and non-constant opcodes still fail to
   compile, and only `nand_write.c` may name `NAND_WCMD`.
2. **Write window.** `nand_bus_cmd_latch_()` panics before touching the bus if a write opcode arrives while the
   write window is closed. The window opens only inside `nand_erase_block()` / `nand_program_page()`, after the arm
   checks pass, and closes before they return, on every path.
3. **Data-input cycle.** `nand_bus_data_in()` panics unless the window is open and the last latched command was `80h`
   followed by 5 addresses. A `check_data_in.sh` rule allows it to be called only from `nand_program_page()`.
4. **WP#.** It is driven high only inside the write window: high, then `t_ww`, then the setup command. It goes back low
   right after R/B# is ready and the status register is read, or at once on timeout or error (which aborts the
   operation, §4.3). `check_wp.sh` allows exactly the boot drive-low, the raise in `nand_write.c`, and the lowers.
   Because WP# is never high outside one operation, a crash between operations cannot leave it high.
5. **Arm state** (RAM only, so every boot starts disarmed). `ARM_WRITE` takes a fixed 32-bit token, a block range
   `[first, last]` and an idle timeout (1–60 s). Erase and program outside the range are rejected without touching
   the bus. The device disarms on `DISARM`, a DTR change, the idle timeout, any failed operation, `RESET`, and the
   end of a `write` job.
6. **Bad-block marker check in firmware.** `ERASE_BLOCK` first reads spare byte 0 (column 2048) of pages 0, 1 and
   63 with ordinary `00h`/`30h` reads. If any is not `FFh` it refuses with `ERR_BAD_BLOCK`. An explicit flag can
   override this, for a block the user deliberately wants to erase anyway.
7. **Watchdog + fault handler.** The fault handler parks the bus and drives WP# low. A watchdog (e.g. 500 ms, fed in
   the main loop and in the R/B# poll) resets the Pico if anything hangs. After reset, GP13 is an input and the 10k
   pull-down holds WP# low.
8. **Post-op status check.** The firmware reads SR (`70h`) before lowering WP#. It reports `ERR_WP_STUCK` if bit 7 = 0
   (WP# never rose, so the chip ignored the command), `ERR_OP_FAILED` if bit 0 = 1, and `ERR_RB_TIMEOUT` on timeout.
   Any of these disarms the device.

## 4. Protocol v2 (changes to `PROTOCOL.md`)

`PROTO_VERSION` 1 → 2. The host refuses a v1 device, as it does today.

- **Request `arg_len` becomes u16** (header 5 bytes, max 2116 = page index + 2112 B). The parser buffer grows to
  2116 B. Everything else in the request and response frames stays the same.
- **`timing_t` gains `t_adl` and `t_ww`** (13 × u16 + u32 = 30 B). Floors: tADL ≥ 70 ns, tWW ≥ 100 ns.
  DEFAULT: `t_adl` 18 cycles (144 ns), `t_ww` 25 cycles (200 ns). Program and erase timeouts are fixed (2 ms / 20 ms),
  not part of `rb_timeout_us`.
- New commands:

| Code | Name | Args | Response |
|---:|---|---|---|
| `0x0A` | `ARM_WRITE` | `u32 token`, `u16 first_block`, `u16 last_block`, `u16 idle_timeout_s` | empty `OK` |
| `0x0B` | `DISARM` | none | empty `OK` (also when not armed) |
| `0x0C` | `ERASE_BLOCK` | `u16 block`, `u8 flags` (bit 0 = ignore bad-block marker) | `u8 sr`, `u32 busy_ns` |
| `0x0D` | `PROGRAM_PAGE` | `u32 page`, 2112 bytes | `u8 sr`, `u32 busy_ns` |

- New status codes: `0x08 ERR_NOT_ARMED` (also for out-of-range blocks), `0x09 ERR_BAD_BLOCK`, `0x0A ERR_OP_FAILED`
  (SR bit 0 = 1; `sr` is still returned), `0x0B ERR_WP_STUCK`.
- One operation per request, with no stream. The host waits for each response, then sends the next. Pipelining (two
  requests in flight) is a later optimisation, only after the write milestones pass.

## 5. Host tool

New subcommands. All of them except `blank` refuse to run unless ID = `01 DA 90 95 44` and `param` passes.

- `nandtool erase --block N [--count M] [--ignore-bad-marker]` erases blocks. It prints the blocks, then asks you to
  type `ERASE <n> BLOCKS`.
- `nandtool program --page N --in FILE` programs one page from a 2112-byte file. It reads the page first and refuses
  unless it is all `FFh`, then reads it back and compares.
- `nandtool write IMAGE [--start-block B --count M] [--map 1:1|skip-bad] [--all] [--backup DUMP] [--yes]` is the
  main command:
  1. **Plan.** Read the target's bad-block markers for the range and save them to `<IMAGE>.target-bbt.json` before
     anything is erased (§9.2). Scan the image's own markers. Build the block map (§6). Unless `--all` is given,
     read each target block and compare it with the image block: identical blocks are skipped. This is
     **only-changed** mode, the default, so a patched image touches only the patched blocks.
  2. **Confirm.** Print counts (blocks to erase, pages to program, blocks skipped, bad blocks) and any warnings. If
     there is no `--backup`, print a loud warning that erased data cannot be recovered without one, and ask for the
     typed confirmation. `--yes` skips the prompt (for scripts). If `--backup` is given, its chip ID is checked and 64
     random pages are spot-checked against the chip, so the backup is shown to belong to *this* chip.
  3. **Per block.** Arm the range, erase the block, program each page that is not all `FFh` (an erased page is
     already `FFh`, so all-`FFh` pages are skipped), then read the whole block back and compare it with the image. A
     mismatch is read again once. If it still differs, the job stops with a report. A raw mismatch is never accepted.
  4. **Resume.** Keep a `.meta.json` next to the image, as `dump` does. Resume restarts from the block that was in
     progress and always re-erases it (§3.2/§3.5).
  5. **Stop on failure.** On `ERR_OP_FAILED`, report the block as grown-bad and stop. It is not marked, remapped or
     skipped automatically; you decide.
- `nandtool badblocks` gains `--device` (scan the chip's markers live) and `--blank` (list blocks that are all
  `FFh` in an image). You need these to pick the sacrificial block for W3.

## 6. Bad blocks and cloning

| Case | `--map 1:1` (default) | `--map skip-bad` |
|---|---|---|
| Image block *i* → target block | *i* | next good target block (Linux `nandwrite` style) |
| Target block bad, image block has data | **refuse before any erase**, listing the conflicts | skipped; the image shifts by one block |
| Target block bad, image block all `FFh` | left untouched | skipped |
| Image block marked bad (source chip) | left untouched; target not erased | dropped from the image |
| Grown bad (program/erase fails) | stop | stop |

Same-chip writes are naturally 1:1 with no conflicts. For cloning, the right mode depends on the original
controller: a positional FTL or BBT needs 1:1, while a bootloader or UBI that skips bad blocks expects skip-bad. The
tool cannot know which, so 1:1 is the default and conflicts stop the job. **ECC/OOB is not recomputed:** the image is
written raw, so a modified data area keeps its old ECC bytes unless you fix them first. That stays out of scope until
SPEC says otherwise.

## 7. Fake device and tests (no hardware)

`fake_device.py` models a real NAND state machine: erase sets the block to `FFh`; program ANDs data in (1 → 0 only)
and counts NOP per page; the arm range and timeout are enforced; `ERR_NOT_ARMED` is returned when disarmed. It can
inject program/erase fails, R/B# timeouts, stuck WP#, and factory bad blocks. Host tests cover the plan, the
1:1/skip-bad maps, only-changed mode, resume after an interrupted block, verify-mismatch handling, typed
confirmation and refusal on conflicts. Firmware `make check` adds the `NAND_WCMD` and data-in call-site rules, and
the timing floors for `t_adl`/`t_ww`.

## 8. Milestones (each hardware step: you run it and paste output)

| # | Goal | Pass criteria |
|---|---|---|
| **W0** | You update `SPEC.md` (§9). I then update the hard rules in `docs/CLAUDE.md`. | SPEC changed by you. |
| **W1** | Code: protocol v2, firmware, host, fake device, gates, CI. No hardware. | `make check`, `.uf2` build, pytest green. |
| **W2** | Regression + interlocks on hardware | M2–M4 checks unchanged (`status --reset` 60h, `id`, `param` CRC `3B C5`, page 0 ×100). `erase` while disarmed → `ERR_NOT_ARMED`, and a block outside the armed range → `ERR_NOT_ARMED`. `badblocks --device` matches the dump. |
| **W3** | First erase, on a sacrificial block that is **all `FFh` in `final.bin`** (picked with `badblocks --blank`) | SR pass, readback all `FFh` ×10, the rest of the chip unchanged (spot-check blocks). |
| **W4** | First program: test patterns into that block (`00`, `FF`, `55`/`AA`, walking bits, random, OOB included) | Readback ×100 identical and equal to the pattern. Then erase → all `FFh` again. |
| **W5** | Same-chip restore of one block with real data: erase + program from `final.bin` | Readback equals `final.bin`. A modified block is then written and written back, ending equal to the original. |
| **W6** | Full chip: `write final.bin` only-changed (expects 0 blocks), then `--all`, then a fresh `dump` | Final dump SHA-256 = that of `final.bin`. Expected about 12 min (5–6 min transfer + program, 5 min verify). |
| **W7** | Clone onto a second, blank chip (when you have one) | `write` with the target BBT scan, then dump = image (except bad blocks). |

## 9. Draft SPEC.md changes (yours to make)

Replace the first two "Hard safety constraints" bullets with something like:

> - The firmware may issue only these NAND commands: `FFh`, `90h`, `ECh`, `00h`/`30h`, `70h` (read side), and, in
>   **write mode only**, `80h`/`10h` (Page Program, full page) and `60h`/`D0h` (Block Erase). Every other opcode,
>   including `85h`, copy-back, cache, multiplane and OTP, stays forbidden at compile time and run time.
> - Write mode is off at boot and is armed at runtime by the host for an explicit block range and a short idle
>   timeout. It disarms on any error, DTR change or timeout. WP# (GP13, 10k pull-down) is driven high only during a
>   single program or erase operation, and low at all other times.
> - The firmware never erases a block whose factory bad-block marker is not `FFh` unless explicitly overridden.
> - The host tool warns loudly and requires typed confirmation before erasing without a verified backup, saves the
>   target's bad-block markers before the first erase, verifies every programmed block by readback, and stops on the
>   first failure.

Also: move "Program/erase" out of "Out of scope" (keep ECC/OOB recomputation there), add milestones W1–W7 (or your
own), and update the goal line ("read-only raw dumper" → dumper and writer).

## 10. Open points for review

1. ARM token: a fixed constant is enough to stop stray or corrupted frames (CRC already covers corruption). It is
   not meant as authentication.
2. The watchdog is new to this firmware. It needs USB to keep working through long erases (≤ 20 ms), which is fine.
3. W3's sacrificial block must be all `FFh` in your dump, so W3–W4 risk no data even before you have a backup
   strategy. `final.bin` on the WSL machine is the backup for W5–W6.
