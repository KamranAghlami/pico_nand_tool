# Pico NAND Tool — project instructions

A Raspberry Pi Pico (RP2040) NAND tool. The current scope (SPEC) is a **read-only** raw dumper. It bit-bangs a
Spansion/SkyHigh **S34ML02G100BHI00** (2 Gb SLC, ×8, 3.3 V, BGA63 in a clamshell socket) and streams raw pages
(2048 data + 64 OOB) over USB to a Python host tool.

The repo is named `pico_nand_tool` (GitHub: KamranAghlami/pico_nand_tool) because other capabilities may come later.
**The name does not relax anything below.** Program/erase stays forbidden until the user changes `docs/SPEC.md`
themselves. Never scaffold, stub or "prepare" write paths in advance.

- Requirements: `docs/SPEC.md` (user-owned; do not edit without being asked).
- Wire protocol (canonical): `docs/PROTOCOL.md`. Approved design record: `docs/PROPOSAL.md`.
- Datasheet: `docs/datasheet.pdf` (doc 002-00676 Rev \*W). **It is the source of truth.** If SPEC and datasheet
  conflict, stop and tell the user. Do not pick one silently.

## Hard safety rules (never relax these, not even behind a flag)

- The firmware may only ever issue these opcodes: `FFh` Reset, `90h` Read ID, `ECh` Read Parameter Page,
  `00h`/`30h` Page Read, `70h` Read Status. The gate is the `NAND_CMD()` `_Static_assert` macro in
  `firmware/src/nand_cmd.h`, plus a runtime allow-list in `nand_bus_cmd_latch_()` that `panic()`s *before*
  touching the bus. Never call `nand_bus_cmd_latch_` directly: `make -C firmware/tests check`
  (`check_gate_calls.sh`) fails the build if any file other than `nand_cmd.h`, and its definition in `nand_bus.c`,
  names it.
  Never write code for `80h 10h 85h 60h D0h` or any other opcode, and never add a data-input (WE# with CLE=ALE=0)
  path.
- WP# (ball C3) is wired to **GP13** with a 10k external pull-down to GND, which holds it low (write-protected). The
  firmware drives it **low only**: first thing in `nand_bus_init()`, it initialises the pin, clears the latch and
  enables the output. Nothing may ever drive it high or pull it up. `PIN_WP`/`MASK_WP` may appear only in `pins.h`
  and those three lines; `make -C firmware/tests check` (`check_wp.sh`) fails the build otherwise, and a
  `_Static_assert` keeps `MASK_WP` out of every bus mask. Driving WP# high is for a future write mode, and that only
  happens after the user changes `docs/SPEC.md`.
- The dump is the only copy of the data. The host tool never overwrites an existing output file without `--force`.
  It never zero-fills or "guesses" pages; unreadable or unstable pages are flagged, not fixed silently.
- Never claim a hardware milestone (M0–M6) passed without the user's pasted output. At each hardware milestone, stop
  and tell the user exactly what to run and what output to expect.

## Workflow

- Work milestone by milestone in the SPEC order: M0 → M6. Correctness before speed. PIO or other optimisation only
  after M5 passes.
- `docs/PROTOCOL.md` is canonical for the wire format. Change it there first, then in the firmware and host
  together. `docs/PROPOSAL.md` is the approved design record (2026-09-30).
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
and runs the host tests on the latest stable Python.

## Memory

Project memory lives in the repo at `docs/memory/` (index: `docs/memory/MEMORY.md`), not in `~/.claude`.
