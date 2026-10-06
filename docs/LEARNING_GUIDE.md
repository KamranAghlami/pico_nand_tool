# Learning guide: how the Pico NAND Tool works

This is a study companion to the code. It explains the architecture, the hardware concepts it relies on, and the
reasoning behind the design, and gives a reading order through the source. The source files carry matching comments,
so you can read the guide and the code side by side.

The authoritative documents are still `SPEC.md` (requirements), `PROTOCOL.md` (wire format), `PROPOSAL.md` and
`WRITE_PROPOSAL.md` (design records) and the datasheet. Datasheet references in the code look like `§3.1`,
`Fig. 6.1` or `Table 20` and point into `docs/datasheet.pdf`.

---

## 1. What the project does, in one paragraph

A Raspberry Pi Pico (RP2040 microcontroller) is wired straight to the pins of a raw NAND flash chip (Spansion/SkyHigh
S34ML02G1, 2 Gbit, 8-bit bus). The Pico firmware has no NAND controller hardware. It **bit-bangs** the chip's bus,
setting and reading individual GPIO pins in software with exact timing, to issue NAND commands and move bytes. A Python
program on the PC (`nandtool`) talks to the Pico over USB with a small binary protocol. It asks for pages, checks every
frame with a CRC, writes the dump to disk safely, compares and reconciles several dumps, and, in write mode, erases and
programs blocks.

```
 ┌──────────────────────────── PC (Python, host/nand_tool) ─────────────────────────────┐
 │  cli.py ── commands: ping, id, read, dump, compare, reconcile, write, ...             │
 │    │        dump.py / reconcile.py / write.py / postproc.py   (jobs, files, policy)    │
 │    ▼                                                                                  │
 │  client.py ── one method per device command; seq matching, retries, resync, streams  │
 │    │        protocol.py  (frame encode/decode, CRC-32, constants: pure, no I/O)      │
 │    ▼                                                                                  │
 │  transport.py ── pyserial port (or tests/fake_device.py in the unit tests)            │
 └────┬─────────────────────────────────────────────────────────────────────────────────┘
      │ USB full-speed, CDC-ACM used as a raw byte pipe (no text, no baud rate)
 ┌────▼────────────────────── Pico (C, firmware/src) ────────────────────────────────────┐
 │  main.c ── super-loop: watchdog, tud_task() (TinyUSB), protocol_poll()               │
 │  protocol.c ── read bytes → frame.c parser → dispatch → cmd_* → send_resp()          │
 │  nand_ops.c (read sequences)        nand_write.c (arm state, erase, program)          │
 │  nand_cmd.h (opcode allow-list gate)                                                  │
 │  nand_bus.c ── one bus cycle at a time: latch cmd/addr, read bytes, data-in, R/B#    │
 │  pins.h / timing.c  (which GPIO is which signal; how long each phase lasts)           │
 └────┬─────────────────────────────────────────────────────────────────────────────────┘
      │ 15 GPIOs: IO0-7, CLE, ALE, WE#, RE#, CE#, WP#, R/B#
 ┌────▼──────────┐
 │  NAND chip    │  S34ML02G1: 2048 blocks × 64 pages × (2048 data + 64 spare) bytes
 └───────────────┘
```

---

## 2. NAND flash crash course (just what this code needs)

### 2.1 Geometry

| Unit | Size | Notes |
|---|---|---|
| page | 2112 bytes = 2048 **data** + 64 **spare** (also called OOB, "out of band") | smallest unit you can read or program |
| block | 64 pages = 135,168 bytes | smallest unit you can **erase** |
| chip | 2048 blocks = 131,072 pages = 276,824,064 bytes | 256 MiB data + 8 MiB spare |

The spare area is ordinary storage that the chip does not interpret. The system that used the chip before (a router,
a TV, ...) keeps ECC codes, bad-block markers and filesystem metadata there. A raw dump keeps it, which is why every
page in a dump is 2112 bytes and not 2048 (`geometry.py`, `postproc.py split`).

### 2.2 The physics that shape the code

- **Erase sets every bit of a block to 1** (all bytes `FFh`).
- **Programming can only turn 1s into 0s.** To write new data you must erase the whole block first. That is why the
  host's `write` works per block: erase, then program its 64 pages (`write.py write_block`).
- A page that should end up all `FFh` needs no programming at all after an erase. The host skips those (`ERASED_PAGE`).
- Some blocks are bad from the factory. The factory marks them with a non-`FFh` byte at spare offset 0 of page 0, 1
  or 63 of the block (datasheet §9.2). **An erase can destroy that mark**, so the code reads the markers before any
  erase, saves them, and refuses to erase marked blocks unless explicitly told to.
- Bits can be "weak": a marginal cell reads differently from one read to the next. That is why the host reads
  everything at least twice and has a majority-vote `reconcile` command.

### 2.3 The bus

The chip has an 8-bit bidirectional data bus (IO0–IO7) and a handful of control lines. A `#` suffix means **active
low**: the signal "happens" when the wire is at 0 V.

| Signal | Direction | Meaning |
|---|---|---|
| CE# | Pico → chip | Chip Enable. Low = the chip listens. High = it ignores everything else. |
| CLE | Pico → chip | Command Latch Enable. High = the byte on IO is a **command** opcode. |
| ALE | Pico → chip | Address Latch Enable. High = the byte on IO is an **address** byte. |
| WE# | Pico → chip | Write Enable. The chip captures IO on its **rising** edge (cmd, address or data in). |
| RE# | Pico → chip | Read Enable. Each low pulse makes the chip drive the next data byte onto IO. |
| R/B# | chip → Pico | Ready/Busy. Open drain: the chip pulls it low while busy (reading a page into its buffer, programming, erasing). A pull-up makes it high otherwise. |
| WP# | Pico → chip | Write Protect. Low = the chip refuses every program and erase. |

Every interaction is made of three kinds of **cycles**:

| Cycle | CLE | ALE | Strobe | Function in the code |
|---|---|---|---|---|
| command latch | 1 | 0 | WE# pulse | `latch_cycle(MASK_CLE, op)` via `NAND_CMD()` |
| address latch | 0 | 1 | WE# pulse | `nand_bus_addr()` |
| data in (write) | 0 | 0 | WE# pulse | `nand_bus_data_in()` (write mode only) |
| data out (read) | 0 | 0 | RE# pulse | `nand_bus_read()` |

A **page read** (datasheet §3.1, Fig. 6.1), as `nand_ops.c nand_read_column()` performs it:

```
 1. CE# low                                       chip selected               nand_bus_select()
 2. command 00h                                   "read setup"                NAND_CMD(NAND_CMD_READ_1)
 3. address × 5: col low, col high,                which page, and where in    nand_bus_addr() ×5
                 row low, row mid, row high        the page to start
 4. command 30h                                   "go"                        NAND_CMD(NAND_CMD_READ_2)
 5. wait tWB, then R/B# goes low for tR (≤ 25 µs) chip copies the page from   nand_bus_wait_ready()
    while the chip loads the page; wait for high   the array into its buffer
 6. 2112 × RE# pulse, sample IO after each fall   stream the buffer out       nand_bus_read()
 7. CE# high                                                                  nand_bus_deselect()
```

Program (`80h` · 5 addresses · 2112 data bytes · `10h` · wait) and erase (`60h` · 3 row addresses · `D0h` · wait) have
the same shape. After either one the code reads the **status register** (command `70h`): bit 0 = failed, bit 6 =
ready, bit 7 = 1 if the chip was *not* write-protected (datasheet Table 13).

### 2.4 Timing

The datasheet (Table 20) gives a **minimum** duration for every phase: how long IO must be stable before WE# rises
(tDS), how long WE# must stay low (tWP), how long after RE# falls the data is valid (tREA), and so on. Going slower is
always allowed; going faster is not. So the firmware treats every delay as a minimum, and any extra instructions or
interrupts can only make the bus slower, which is safe. All delays live in one runtime struct, `timing_t`, measured in
CPU clock cycles (8 ns at 125 MHz). See `timing.h` and `PROTOCOL.md` "timing_t".

### 2.5 ONFI parameter page

The chip can describe itself: command `ECh` returns a 256-byte **parameter page** (three identical copies for
redundancy) with a standard layout (ONFI): manufacturer, model, geometry, timings, and a CRC-16 at bytes 254–255.
`nandtool param` reads and checks it. It is the best wiring test: a single bad data line breaks the CRC.

---

## 3. Reading order

Read bottom-up for the firmware (hardware → protocol), then top-down for the host (CLI → transport):

| # | File | What to learn there |
|---:|---|---|
| 1 | `firmware/src/pins.h` | the pin map; bit masks for the RP2040's single-cycle GPIO set/clear registers |
| 2 | `firmware/src/timing.h`, `timing.c` | the `timing_t` struct; how datasheet nanoseconds become cycles; floors |
| 3 | `firmware/src/nand_cmd.h` | compile-time allow-list of NAND opcodes (`_Static_assert` in a macro) |
| 4 | `firmware/src/nand_bus.h`, `nand_bus.c` | bit-banging: latch cycles, read cycles, R/B# polling, the write window and its sequence state machine |
| 5 | `firmware/src/nand_ops.c` | datasheet command sequences for reading (reset, ID, status, param, page) |
| 6 | `firmware/src/nand_write.c` | write mode: arm state, erase, program, status check |
| 7 | `firmware/src/crc32.c`, `frame.h`, `frame.c` | CRC-32 and the byte-at-a-time request parser |
| 8 | `firmware/src/protocol.c` | request dispatch, response framing, USB flow control, the READ_PAGES stream |
| 9 | `firmware/src/main.c`, `fault.c` | the super-loop, watchdog and panic handling |
| 10 | `host/nand_tool/protocol.py` | the same wire format in Python (`struct` formats) |
| 11 | `host/nand_tool/transport.py` | the serial port behind a tiny interface (`typing.Protocol`) |
| 12 | `host/nand_tool/client.py` | request/response matching, retry and resync, streaming generator, write pipelining |
| 13 | `host/nand_tool/dump.py`, `compare.py`, `reconcile.py` | safe dumping, resume, diffing, majority vote |
| 14 | `host/nand_tool/write.py` | plan-then-run writing, bad-block maps, the sidecar |
| 15 | `host/nand_tool/cli.py` | argparse wiring and user-facing output |
| 16 | `host/tests/fake_device.py` | a whole simulated device: how the host is tested without hardware |

---

## 4. Life of a request: `nandtool read --page 5`

Following one command end to end touches almost every layer.

**Host side**

1. `cli.main()` parses the arguments. `read` needs a device, so it calls `transport.open_transport(None)`. That finds
   the port by USB VID:PID `2E8A:000A` and product string "Pico NAND Tool", opens it with pyserial, and wraps it in a
   `Client`.
2. `cmd_read()` iterates `client.read_pages(5, 1)`, a **generator** that yields `(page, data)` pairs.
3. `read_pages()` picks the next sequence number (`seq`) and sends one request frame built by
   `protocol.encode_request(Cmd.READ_PAGES, seq, pack("<II", 5, 1))`:

   ```
   A5 | 08 | seq | 08 00 | 05 00 00 00  01 00 00 00 | CRC-32 (4 bytes, little-endian)
   magic cmd       arg_len   start=5      count=1
   ```

**USB**

4. The bytes go out as USB bulk packets. On the Pico, TinyUSB's driver (run by `tud_task()`) puts them in a receive
   FIFO.

**Firmware side**

5. `main()`'s loop calls `protocol_poll()` → `poll_input()`. It pulls bytes from the FIFO with `tud_cdc_read()` and
   feeds them one at a time to `frame_parser_feed()`. When a whole frame has arrived and its CRC matches, it calls
   `dispatch()` → `cmd_read_pages()`.
6. `cmd_read_pages()` loops over the pages. Before each page it services USB and checks for an incoming `ABORT` (a
   *nested* `poll_input()` call). Then `nand_read_page(5, ...)` runs the bus sequence from §2.3 and fills a 2112-byte
   buffer.
7. `send_resp()` builds the 10-byte header (`frame_resp_header`), computes the CRC over header + payload, and pushes
   it all out with `usb_write_all()`, which waits (running `tud_task()`) whenever the USB transmit FIFO is full.
   Nothing is ever dropped.
8. After the last page it sends an **end frame** (`cmd | 0x80`) carrying `pages_sent` and `pages_failed`.

**Back on the host**

9. `Client.read_response()` reads exactly 10 header bytes, learns the payload length, reads the rest, and
   `decode_response()` checks magic and CRC. `read_pages()` checks that the `seq` is ours and that the page number is
   the one expected next, then yields `(5, data)`.
10. The end frame is checked by `_check_end()`. `cmd_read()` prints the result.

If anything is corrupted on the way (bad CRC, bad magic, a page out of order), the host follows the **recovery rule**:
send `ABORT`, discard input until the line is quiet, and re-request from the first page it has not accepted yet. It
never stitches bytes together across an error.

---

## 5. The wire protocol in one page

(Full definition: `PROTOCOL.md`. Constants are duplicated in `protocol_defs.h` and `protocol.py`, and
`host/tests/test_protocol.py` parses the C header to prove the two agree.)

```
request  (host → Pico):  A5  cmd  seq  arg_len(u16)  args[arg_len]           crc32(u32)
response (Pico → host):  5A  cmd  seq  status  page(u32)  len(u16)  payload[len]  crc32(u32)
```

- **Magic bytes** (`A5`, `5A`) let a receiver find the start of a frame in a byte stream.
- **seq** is chosen by the host and echoed back, so the host can tell the answer to *this* request from a stale
  answer to an earlier one. The host starts at a random seq so a leftover reply from a previous run can't match.
- **CRC-32** (the zlib/Ethernet one) on every frame catches corrupted bytes.
- Every request gets exactly one response, except `READ_PAGES`, which streams one frame per page and then an end
  frame. While a stream runs, the Pico still parses incoming requests between pages: `ABORT` stops the stream, and
  anything else gets an immediate `ERR_BUSY`.
- A **DTR change** (the host opens or closes the port) starts a new "session" on the Pico: leftovers are discarded,
  write mode is disarmed, and a half-sent response is abandoned (`tud_cdc_line_state_cb` in `protocol.c`).

---

## 6. Safety architecture (defence in depth)

Reading a chip is harmless. Erasing or programming destroys data, and the dump may be the only copy of it. So write
capability is fenced in by several independent layers. Any one of them would stop an accident on its own.

| # | Layer | Where | What it stops |
|---:|---|---|---|
| 1 | **Hardware** pull-down on WP# (10 kΩ to GND) | board | With the Pico unplugged, reset or crashed, WP# is low, so the chip refuses program/erase. |
| 2 | **WP# driven low first thing at boot**, and kept out of every bus mask | `nand_bus_init()`, `pins.h` `_Static_assert` | No ordinary bus operation can raise WP#. |
| 3 | **Compile-time opcode allow-list** | `nand_cmd.h`: `NAND_CMD()` / `NAND_WCMD()` | Code that tries to send any other opcode does not compile. |
| 4 | **Run-time opcode gate + sequence state machine** | the command-latch function in `nand_bus.c` | Panics before touching the bus on a forbidden opcode, a write opcode outside the write window, or a write sequence out of order. |
| 5 | **Build-time grep checks** | `firmware/tests/check_gate_calls.sh`, `check_wp.sh` (run by `make check` and CI) | The write path and the WP# pin may only be used in their designated places. The Makefile also proves that the checkers catch planted violations. |
| 6 | **Arm state** | `nand_write.c` | Erase/program are refused (`ERR_NOT_ARMED`) unless the host armed this block range with a magic token, within an idle timeout. RAM-only, so a reboot disarms. |
| 7 | **Bad-block check before erase** | `nand_erase_block()` | Won't erase a factory-marked block unless the request says so explicitly. |
| 8 | **Fail-safe faults** | `fault.c`, watchdog in `main.c` | A panic or HardFault parks the bus with WP# low and halts. A hang reboots the Pico, and after a reboot the pull-down holds WP# low. |
| 9 | **Host policy** | `cli.py`, `write.py`, `client.py` | Typed confirmation, markers saved before the first erase, a lost response never re-sent, optional readback verify, stop at the first failure. |

A useful exercise: for each layer, find the code and work out what would happen if just that layer were missing.

---

## 7. Firmware design notes

- **No RTOS, no interrupts of our own.** `main()` is a *super-loop*: feed the watchdog, run TinyUSB (`tud_task()`),
  handle requests (`protocol_poll()`). Long operations (a stream of 131,072 pages) call `tud_task()` and
  `watchdog_update()` themselves so USB keeps moving.
- **Code runs from RAM** (`__not_in_flash_func`). The RP2040 normally executes from external QSPI flash through a
  cache. A cache miss stalls the CPU for a flash fetch, which would make bus timing slower and less predictable.
- **SIO mask operations.** The RP2040 has single-cycle `GPIO_OUT_SET`/`CLR`/`XOR` registers. `gpio_set_mask(m)` raises
  all pins in `m` at once with one store, so CLE and the data byte change together, with no per-pin loop.
- **Big buffers are `static`.** The RP2040 SDK gives `main` a 2 KB stack. A 2 KB request struct or a 2112-byte page
  on the stack would overflow it.
- **Pure modules are tested on the PC.** `crc32.c`, `frame.c` and `timing.c` include no SDK headers, so
  `firmware/tests/test_host.c` compiles them with the PC's gcc.
- **Throughput.** Reads reach ~850 KiB/s (USB IN is double-buffered). Writes reach ~360 kB/s. The RP2040's TinyUSB
  driver single-buffers OUT endpoints, so the host keeps two `PROGRAM_PAGE` requests in flight to hide the round trip
  (`Client.program_pages`).

## 8. Host design notes

- **Layering.** `protocol.py` is pure (bytes in, objects out). `transport.py` moves bytes. `client.py` adds meaning
  (one method per command, retries). The job modules (`dump`, `reconcile`, `write`) add policy and files. `cli.py`
  only parses arguments and prints. Each layer is testable on its own.
- **The dump is the only copy of the data.** So: never overwrite without `--force`; write chunk by chunk and
  `fsync`; keep a JSON **sidecar** (`FILE.meta.json`) with the range, chip ID, firmware, timing and progress; update
  the sidecar atomically (write a temp file, then `os.replace`); allow `--resume`; never fill a missing page with
  zeros. A page that keeps failing stops the dump.
- **Reconcile** (`reconcile.py`): pages that differ between two dumps are re-read K times, and each bit takes the
  majority value. If the majority isn't clear enough, the page is flagged `unstable` in the report and the exit status.
  It is never silently "fixed".
- **Write = plan, then run** (`write.py`). `build_plan()` reads the target first (bad-block markers, and in
  only-changed mode every block) and decides per block: write, same, bad, conflict. `--dry-run` stops there.
  `run_plan()` then writes block by block, recording the block in progress in a sidecar so an interrupted run re-does
  exactly that block.
- **Never re-send a write blindly.** If the response to an erase or program is lost, the host cannot know whether it
  ran. `Client.request(resend=False)` raises `LostResponse`, and `write_block()` re-does the *whole block* (erase +
  program), which is always correct.

## 9. Testing strategy

| What | How | Command |
|---|---|---|
| Firmware pure modules | `firmware/tests/test_host.c`, compiled with the PC's gcc | `make -C firmware/tests check` |
| Opcode gate | `forbidden_opcode.c` must **fail** to compile for forbidden opcodes (negative tests), and compile for allowed ones (positive control) | same |
| Gate / WP# usage | `check_gate_calls.sh`, `check_wp.sh`, plus planted "sneaky" sources that must be caught | same |
| Host logic | pytest against `tests/fake_device.py`, an in-process simulation of the firmware, with injectable faults (dropped/corrupted frames, bit flips, R/B# timeouts, failing erases) | `.venv/bin/pytest host/tests -q` |
| Protocol constants | `test_protocol.py` parses `protocol_defs.h` and compares it with `protocol.py` | same |
| Hardware | milestones M0–M6, W1–W7, run by hand and recorded in `docs/memory/milestone-status.md` | — |

## 10. Exercises

1. Draw the waveform of `nand_read_status()` (dummy `00h`, `70h`, one byte out) and mark where each `timing_t` field
   applies.
2. In `nand_bus.c`, trace the `wseq` state for a program: which calls move it through `WSEQ_PROG_ADDR` →
   `WSEQ_PROG_DATA` → `WSEQ_PROG_LOADED`? What happens if someone sends six address bytes?
3. In `frame.c`, feed the parser `A5 A5 01 00 00 ...` by hand. Why does a false magic byte not lose the real frame
   that follows it?
4. In `client.py`, why does `read_pages()` keep the `streaming` flag, and what does the `finally:` block protect
   against?
5. In `reconcile.py`, compute `default_min_agree(5)` and explain why `ones == zeros` is always "unstable".
6. Add a fault to `fake_device.py` (e.g. drop every 100th page frame) and watch `Client.read_pages` recover in a test.

## 11. Glossary

| Term | Meaning |
|---|---|
| bit-bang | drive a bus protocol from software by toggling GPIO pins, instead of with dedicated hardware |
| OOB / spare | the 64 extra bytes per page, outside the 2048 "data" bytes |
| R/B# | the chip's Ready/Busy output (low = busy) |
| tR, tPROG, tBERS | chip-internal busy times for page read (≤ 25 µs), program (≤ 700 µs), block erase (≤ 10 ms) |
| SR | status register, read with command `70h` |
| ONFI | Open NAND Flash Interface, the standard behind the parameter page |
| CDC-ACM | the USB "virtual serial port" class; used here as a plain byte pipe |
| DTR | a serial-port control line; the host asserts it when it opens the port, so the firmware uses it to detect sessions |
| seq | per-request sequence number, echoed in the response |
| sidecar | a JSON file next to a dump or image that records how it was made and how far a job got |
| arm | the write-mode permission the host must grant (token + block range + timeout) before erase/program |
| write window | the stretch of one erase or program during which the firmware holds WP# high |
