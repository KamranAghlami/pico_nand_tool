# Pico NAND Dumper — project instructions

A **read-only** raw NAND dumper. A Raspberry Pi Pico (RP2040) bit-bangs a Spansion/SkyHigh **S34ML02G100BHI00**
(2 Gb SLC, ×8, 3.3 V, BGA63 in a clamshell socket) and streams raw pages (2048 data + 64 OOB) over USB to a Python
host tool.

- Requirements: `docs/SPEC.md` (user-owned; do not edit without being asked).
- Design under review: `docs/PROPOSAL.md` (repo layout, wire protocol, timing defaults).
- Datasheet: `docs/datasheet.pdf` (doc 002-00676 Rev \*W). **It is the source of truth.** If SPEC and datasheet
  conflict, stop and tell the user. Do not pick one silently.

## Hard safety rules (never relax these, not even behind a flag)

- The firmware may only ever issue these opcodes: `FFh` Reset, `90h` Read ID, `ECh` Read Parameter Page,
  `00h`/`30h` Page Read, `70h` Read Status. The gate is the `nand_cmd_t` enum plus the `NAND_CMD()` `_Static_assert`
  macro, with a runtime allow-list that `panic()`s *before* touching the bus.
  Never write code for `80h 10h 85h 60h D0h` or any other opcode, and never add a data-input (WE# with CLE=ALE=0)
  path.
- WP# is hard-wired to GND. There is no GPIO for it and there must never be one.
- The dump is the only copy of the data. The host tool never overwrites an existing output file without `--force`.
  It never zero-fills or "guesses" pages; unreadable or unstable pages are flagged, not fixed silently.
- Never claim a hardware milestone (M0–M6) passed without the user's pasted output. At each hardware milestone, stop
  and tell the user exactly what to run and what output to expect.

## Workflow

- Work milestone by milestone in the SPEC order: M0 → M6. Correctness before speed. PIO or other optimisation only
  after M5 passes.
- The proposal in `docs/PROPOSAL.md` must be approved before bulk code is written. Once approved, move the protocol
  into `docs/PROTOCOL.md` and keep it canonical. Change it there first, then in the code.
- Protocol constants live in `firmware/src/protocol_defs.h` **and** `host/nand_dumper/protocol.py`. A pytest keeps
  them in sync, so update both together.
- Comment every NAND bus sequence with the datasheet section, figure or table it implements (e.g.
  `/* §3.1, Fig. 6.1 */`).
- Keep the firmware small and readable. Pin numbers live only in `firmware/src/pins.h`.

## Firmware conventions (C, Pico SDK 2.x, TinyUSB)

- Use SIO mask ops only (`gpio_set_mask`, `gpio_clr_mask`, `gpio_put_masked`, `gpio_set_dir_*_masked`), never
  per-pin loops. The data bus is GP0–GP7 and is read with `gpio_get_all() & 0xFF`.
- Init order: clear the RP2040 default pull-downs, set CE#/WE#/RE# **high in the output latch first**, then enable
  them as outputs.
- The data bus is an output only during cmd/addr cycles. Switch it to input before the first RE#↓ (respect tWHR),
  and back to output only after tRHW.
- All delays come from the single runtime `timing_t` (clk_sys cycles, 8 ns at 125 MHz). There are DEFAULT and SLOW
  (1 µs/phase) presets, and floors that `SET_TIMING` enforces. Bus code runs from RAM (`__not_in_flash_func`).
- R/B#: wait ≥ tWB after the last WE#↑, then poll with a timeout (1 ms; 10 ms for the power-on wait before RESET).
  A timeout is an error status. Never hang.
- USB: TinyUSB CDC used **raw** (`tud_cdc_read`/`tud_cdc_write`). Never use `stdio_usb`/`printf`, and never put
  text on the data channel.

## Host conventions (Python ≥ 3.10, pyserial, pytest)

- Package `host/nand_dumper`, CLI `nandd`. Everything hardware-independent is unit-tested against
  `host/tests/fake_device.py`, which simulates the firmware protocol and supports injectable transport errors and
  bitflips.
- Reference parameter page: rebuilding Table 3.4 for S34ML02G100 ×8 gives ONFI CRC `0xC53B` (bytes 254–255 =
  `3B C5`). Use it as the test fixture.
- Never modify the system Python. Use a venv: `python3 -m venv .venv && .venv/bin/pip install -e 'host[test]'`.

## Commands

```sh
# Firmware (needs arm-none-eabi-gcc, cmake, ninja, PICO_SDK_PATH → pico-sdk 2.3.1)
cmake -S firmware -B build -G Ninja -DPICO_BOARD=pico && cmake --build build   # → build/*.uf2

# Host
python3 -m venv .venv && .venv/bin/pip install -e 'host[test]'
.venv/bin/pytest host/tests -q
```

CI (`.github/workflows/ci.yml`) builds the firmware, checks that a forbidden opcode fails to compile, and runs the
host tests on Python 3.10, 3.12 and 3.14. Each job skips itself until its directory exists.

## Memory

Project memory lives in the repo at `docs/memory/` (index: `docs/memory/MEMORY.md`), not in `~/.claude`.
