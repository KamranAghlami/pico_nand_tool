# pico_nand_tool

A Raspberry Pi Pico (RP2040) based raw NAND tool. Its current scope is a **read-only** dumper: the Pico bit-bangs a
Spansion/SkyHigh S34ML02G1 (2 Gb SLC, ×8, BGA63) and streams full raw pages (data + OOB) over USB to a Python host
tool. The host tool verifies, retries, compares and reconciles multi-pass dumps.

- Requirements: [`docs/SPEC.md`](docs/SPEC.md)
- Wire protocol: [`docs/PROTOCOL.md`](docs/PROTOCOL.md)
- Design record (approved): [`docs/PROPOSAL.md`](docs/PROPOSAL.md)

In its current scope the firmware never programs or erases the chip. Only Reset, Read ID, Read Parameter Page, Page
Read and Read Status can reach the bus, and WP# is hard-wired to GND.

**Status:** milestone M0 (toolchain + USB) has passed on hardware. M1 (bus test) is next. The firmware does not
talk to the NAND chip yet; see [Usage](#usage) for what works today.

## Hardware

### Parts

- Raspberry Pi Pico (RP2040). The firmware targets the plain Pico, not the Pico W.
- BGA63 (9 × 11 mm) clamshell socket holding the S34ML02G100BHI00.
- 4 × 10 kΩ pull-up resistors to 3V3: WE#, RE#, CE#, R/B#.
- 100 nF + 10 µF decoupling capacitors at the socket, VCC to VSS.

### Wiring (Pico → NAND BGA63 ball)

The pin map lives in [`firmware/src/pins.h`](firmware/src/pins.h). Change it there only.

| Pico GPIO | Pico pin | Signal | NAND ball | Notes |
|---|---:|---|---|---|
| GP0 | 1 | I/O0 | H4 | |
| GP1 | 2 | I/O1 | J4 | |
| GP2 | 4 | I/O2 | K4 | |
| GP3 | 5 | I/O3 | K5 | |
| GP4 | 6 | I/O4 | K6 | |
| GP5 | 7 | I/O5 | J7 | |
| GP6 | 9 | I/O6 | K7 | |
| GP7 | 10 | I/O7 | J8 | |
| GP8 | 11 | CLE | D5 | |
| GP9 | 12 | ALE | C4 | |
| GP10 | 14 | WE# | C7 | 10 kΩ pull-up to 3V3 |
| GP11 | 15 | RE# | D4 | 10 kΩ pull-up to 3V3 |
| GP12 | 16 | CE# | C6 | 10 kΩ pull-up to 3V3 |
| GP14 | 19 | R/B# | C8 | open drain, 10 kΩ pull-up to 3V3 |
| — | — | **WP#** | C3 | **tie to GND**. Never connect it to the Pico. |
| 3V3(OUT) | 36 | VCC | D3, G4, H8, J6 | 100 nF + 10 µF at the socket |
| GND | 3, 8, 13, … | VSS | C5, F7, K3, K8 | |

- I/O0–I/O7 must stay on 8 consecutive GPIOs, so that one read of the GPIO register gives the data byte.
- Datasheet Fig. 3 says balls D3, G4 (VCC) and F7 (VSS) "might not be bonded internally". Connect them anyway, but
  make sure the guaranteed balls, **H8 and J6 (VCC)** and **C5, K3 and K8 (VSS)**, are solid.
- With WP# grounded, the chip's status register reads `60h` after a reset. `E0h` would mean WP# is not really
  grounded: stop and check the wiring.

## Build the firmware

You can download a prebuilt `.uf2` from CI, or build it yourself.

**Download from CI** (needs the [GitHub CLI](https://cli.github.com/)):

```sh
gh run list -R KamranAghlami/pico_nand_tool -L 5                  # pick the latest successful run ID
gh run download <run-id> -R KamranAghlami/pico_nand_tool -n pico-nand-tool-uf2 -D /tmp/fw
```

**Build locally:** you need an ARM GCC toolchain, CMake, Ninja, Git and Pico SDK **2.3.1**. On Ubuntu/Debian:

```sh
sudo apt install gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib \
                 cmake ninja-build build-essential git pkg-config libusb-1.0-0-dev

git clone --depth 1 --branch 2.3.1 https://github.com/raspberrypi/pico-sdk.git ~/pico-sdk
git -C ~/pico-sdk submodule update --init --depth 1 lib/tinyusb
export PICO_SDK_PATH=~/pico-sdk

cmake -S firmware -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

The result is `build/pico_nand_tool.uf2`. On the first build the SDK also fetches and builds `picotool`.

## Flash the Pico

1. Hold **BOOTSEL** while plugging the Pico into USB. A drive called `RPI-RP2` appears.
2. Copy `pico_nand_tool.uf2` onto it. The Pico reboots into the firmware, and its LED lights once USB is enumerated.

To reflash later without pressing BOOTSEL, set the serial port to 1200 baud. The firmware then reboots into
BOOTSEL:

```sh
stty -F /dev/ttyACM0 1200     # Linux
```

Check that it enumerated: `lsusb -d 2e8a:000a` should list the device, and `nandtool ping` should answer (see
[Usage](#usage)).

## Install the host tool

You need Python 3.10 or newer. Install the tool into a virtual environment, never into the system Python:

```sh
python3 -m venv .venv
.venv/bin/pip install -e host            # or -e 'host[test]' to also get pytest
```

This installs the `nandtool` command into `.venv/bin/`. Run `source .venv/bin/activate` to call it as just
`nandtool`.

- **Linux:** if opening the port fails with "permission denied", add yourself to the `dialout` group
  (`sudo usermod -aG dialout $USER`), then log out and back in.
- **WSL2:** attach the Pico from Windows with [usbipd-win](https://github.com/dorssel/usbipd-win). From an admin
  PowerShell, run `usbipd list`, then `usbipd bind --busid <id>` once, then `usbipd attach --wsl --busid <id>`. It then
  appears as `/dev/ttyACM0`. Attach again after every reflash or replug.
- **Windows (native):** auto-detect can't tell the tool apart from other Picos there, so always pass `--port COMx`.

## Usage

```
nandtool [--port PORT] [--timeout SECONDS] <command> [options]
```

| Global option | Meaning |
|---|---|
| `--port PORT` | serial port, e.g. `/dev/ttyACM0` or `COM5`. Default: auto-detect USB `2E8A:000A` with product "Pico NAND Tool" |
| `--timeout S` | per-response timeout in seconds (default 1.0) |
| `--version` | host tool and protocol version |

Exit status: 0 = success, 1 = error (printed as `error: …`), 130 = interrupted with Ctrl-C.

### Commands available now

| Command | What it does |
|---|---|
| `nandtool ping` | Liveness check. Prints the firmware version, the protocol version and clk_sys. |
| `nandtool timing` | Shows the active bus timing: every delay in cycles and ns, and the R/B# timeout. |
| `nandtool timing --slow` | Selects the SLOW preset (1 µs per bus phase), so a 24 MHz logic analyzer can resolve every edge. |
| `nandtool timing --default` | Back to the DEFAULT preset (≥ 2× every datasheet minimum). |

The timing setting lives in the Pico's RAM. It resets to DEFAULT when the Pico reboots.

Example:

```
$ nandtool ping
firmware : pico-nand-tool 0.1.0 (a749da2)
version  : 0.1.0, protocol v1
clk_sys  : 125.000 MHz
```

### Planned commands, by milestone

| Milestone | Commands |
|---|---|
| M1 | `bus-test`: toggles every control and data line in a fixed order (CE# stays high, so it is chip-safe) |
| M2 | `status`, `id` (expects `01 DA 90 95 44`) |
| M3 | `param`: reads and checks all 3 ONFI parameter-page copies (CRC `3B C5`), decodes the geometry |
| M4 | `read --page N [--repeat K]` |
| M5 | `dump --out FILE [--start N --count M] [--resume] [--force]`, `compare A B`, `reconcile A B --out FINAL` |
| M6 | `split IMAGE` (→ `data.bin` + `oob.bin`), `badblocks IMAGE` |

## Tests

```sh
make -C firmware/tests check        # firmware unit tests + opcode safety check (host gcc, no SDK needed)
.venv/bin/pytest host/tests -q      # host tool against a simulated device (needs host[test])
```

CI runs both on every push, builds the `.uf2`, and uploads it as the `pico-nand-tool-uf2` artifact.
