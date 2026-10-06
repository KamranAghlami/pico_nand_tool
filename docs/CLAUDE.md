# Pico NAND Tool — project instructions

A Raspberry Pi Pico (RP2040) NAND tool. It bit-bangs a Spansion/SkyHigh **S34ML02G100BHI00** (2 Gb SLC, ×8,
3.3 V, BGA63 in a clamshell socket) and streams raw pages (2048 data + 64 OOB) over USB to a Python host tool. Read
side: M0–M6, done. **Write mode** (block erase + full-page program, armed at runtime) was added to the SPEC on
2026-10-05; design record `docs/WRITE_PROPOSAL.md`, milestones W1–W7.

Write mode exists, but only within the hard rules below. Any write capability beyond them (more opcodes, partial
pages, WP# handling changes, skipping the arm) needs a SPEC change by the user first.

- Requirements: `docs/SPEC.md` (user-owned; do not edit without being asked).
- Wire protocol (canonical): `docs/PROTOCOL.md`. Approved design records: `docs/PROPOSAL.md` (read side),
  `docs/WRITE_PROPOSAL.md` (write mode).
- Study guide: `docs/LEARNING_GUIDE.md` (architecture, NAND primer, reading order). The source carries matching
  explanatory comments; keep both in step when the architecture changes.
- Datasheet: `docs/datasheet.pdf` (doc 002-00676 Rev \*W). **It is the source of truth.** If SPEC and datasheet
  conflict, stop and tell the user. Do not pick one silently.

## Hard safety rules (never relax these, not even behind a flag)

- The firmware may only ever issue these opcodes. Read side: `FFh` Reset, `90h` Read ID, `ECh` Read Parameter
  Page, `00h`/`30h` Page Read, `70h` Read Status, through `NAND_CMD()`. Write side: `80h`/`10h` Page Program and
  `60h`/`D0h` Block Erase, through `NAND_WCMD()`. Both are `_Static_assert` macros in `firmware/src/nand_cmd.h`.
  `nand_bus_cmd_latch_()` re-checks at run time and `panic()`s *before* touching the bus on any other opcode, on a
  write opcode outside the write window, and on a write sequence out of order (`10h` only after `80h` + 5 addresses
  + data, `D0h` only after `60h` + 3 addresses). Never add `85h`, copy-back, cache, multiplane, `78h`, OTP or any
  other opcode.
- Never call `nand_bus_cmd_latch_` directly. `NAND_WCMD`, `nand_bus_data_in` and `nand_bus_write_window_open/close`
  may be used only in `nand_write.c` (plus their declarations and definitions). `make -C firmware/tests check`
  (`check_gate_calls.sh`) fails the build otherwise.
- The data-input cycle (WE# with CLE = ALE = 0) exists only in `nand_bus_data_in()`, which panics unless the write
  window is open and `80h` + 5 address cycles came just before. There is no other data-input path.
- Write mode is armed only by the host's `ARM_WRITE` (token + block range + idle timeout ≤ 60 s). The arm state is
  RAM-only (boot = disarmed) and is dropped on `DISARM`, a DTR change, `RESET`, the idle timeout and any failed
  write operation. Erase/program of a block outside the armed range is rejected before the bus is touched.
- WP# (ball C3) is on **GP13** with a 10k external pull-down to GND. The firmware drives it low first thing in
  `nand_bus_init()`. It goes high in exactly one place, `nand_bus_write_window_open()`, and low again in
  `nand_bus_write_window_close()` and `nand_bus_park()` (panic/fault path). The write window spans exactly one
  erase or program operation and is closed on every return path, including timeouts (WP# low aborts the operation,
  §4.3). Nothing pulls WP# up. `PIN_WP`/`MASK_WP` may appear only in `pins.h` and those lines in `nand_bus.c`;
  `check_wp.sh` enforces that, and a `_Static_assert` keeps `MASK_WP` out of every bus mask.
- Panic and HardFault park the bus (WP# low) and stop the watchdog, then halt. The watchdog resets a hung Pico; after
  reset GP13 is an input and the pull-down holds WP# low.
- `ERASE_BLOCK` checks the factory bad-block marker (§9.2) itself and refuses unless the host sets the explicit
  override flag for that request.
- The dump is the only copy of the data. The host tool never overwrites an existing output file without `--force`.
  It never zero-fills or "guesses" pages; unreadable or unstable pages are flagged, not fixed silently.
- The host tool never erases without typed confirmation or `--yes`, saves the target's bad-block markers before the
  first erase, checks the status of every erase/program, verifies every written block by readback when asked
  (`--verify`, off by default since 2026-10-05 by the user's SPEC change; a run without it says so), and stops at
  the first failure. It never re-sends an erase or program request automatically after a lost response (it re-does
  the whole block instead), and never marks, remaps or skips a failing block silently.
- Never claim a hardware milestone (M0–M6, W2–W7) passed without the user's pasted output. At each hardware
  milestone, stop and tell the user exactly what to run and what output to expect.

## Workflow

- Work milestone by milestone in the SPEC order: M0 → M6, then W1 → W7. Correctness before speed. PIO or other
  optimisation only after M5 passes (and, for writing, after W6).
- `docs/PROTOCOL.md` is canonical for the wire format. Change it there first, then in the firmware and host
  together. `docs/PROPOSAL.md` (2026-09-30) and `docs/WRITE_PROPOSAL.md` (2026-10-05) are the approved design
  records.
- Protocol constants live in `firmware/src/protocol_defs.h` **and** `host/nand_tool/protocol.py`. A pytest keeps
  them in sync, so update both together.
- Comment every NAND bus sequence with the datasheet section, figure or table it implements (e.g.
  `/* §3.1, Fig. 6.1 */`).
- Keep the firmware small and readable. Pin numbers live only in `firmware/src/pins.h`.

## Firmware conventions (C, Pico SDK 2.x, TinyUSB)

- Use SIO mask ops only (`gpio_set_mask`, `gpio_clr_mask`, `gpio_put_masked`, `gpio_set_dir_*_masked`), never
  per-pin loops. The data bus is GP0–GP7 and is read with `gpio_get_all() & 0xFF`.
- Init order: drive WP# (GP13) low, clear the RP2040 default pull-downs, set CE#/WE#/RE# **high in the output latch
  first**, then enable them as outputs.
- The data bus is an output only during cmd/addr cycles. Switch it to input before the first RE#↓ (respect tWHR),
  and back to output only after tRHW.
- All delays come from the single runtime `timing_t` (clk_sys cycles, 8 ns at 125 MHz). There are DEFAULT and SLOW
  (1 µs/phase) presets, and floors that `SET_TIMING` enforces. Bus code runs from RAM (`__not_in_flash_func`).
- R/B#: wait ≥ tWB after the last WE#↑, then poll with a timeout (1 ms; 10 ms for the power-on wait before RESET).
  A timeout is an error status. Never hang.
- USB: TinyUSB CDC used **raw** (`tud_cdc_read`/`tud_cdc_write`). Never use `stdio_usb`/`printf`, and never put
  text on the data channel.

## Host conventions (Python ≥ 3.10, pyserial, pytest)

- Package `host/nand_tool` (`nand_tool`), CLI `nandtool`. Everything hardware-independent is unit-tested against
  `host/tests/fake_device.py`, which simulates the firmware protocol and supports injectable transport errors and
  bitflips.
- Reference parameter page: rebuilding Table 3.4 for S34ML02G100 ×8 gives ONFI CRC `0xC53B` (bytes 254–255 =
  `3B C5`). Use it as the test fixture.
- Never modify the system Python. Use a venv: `python3 -m venv .venv && .venv/bin/pip install -e 'host[test]'`.

## Commands

```sh
# Firmware (needs arm-none-eabi-gcc, cmake, ninja, PICO_SDK_PATH → pico-sdk 2.3.1)
cmake -S firmware -B build -G Ninja && cmake --build build     # → build/pico_nand_tool.uf2

# Firmware checks on the host (no SDK): pure-module unit tests + opcode safety gate
make -C firmware/tests check

# Host tool
python3 -m venv .venv && .venv/bin/pip install -e 'host[test]'
.venv/bin/pytest host/tests -q
.venv/bin/nandtool ping
```

CI (`.github/workflows/ci.yml`) runs `make -C firmware/tests check`, builds the `.uf2` (uploaded as an artifact),
and runs the host tests on the latest stable Python. A `vX.Y.Z` tag builds the firmware with `-DFW_VERSION=X.Y.Z`
and the host with `__version__ = "X.Y.Z"` (`nand_tool/__init__.py`, the host version's only source), then publishes
a GitHub release with `pico_nand_tool-vX.Y.Z.uf2` and the host wheel. Keep both defaults (CMakeLists `FW_VERSION`,
`__version__`) equal to the latest tag.

## Memory

Project memory lives in the repo at `docs/memory/` (index: `docs/memory/MEMORY.md`), not in `~/.claude`.
