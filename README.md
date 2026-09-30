# pico_nand_tool

A Raspberry Pi Pico (RP2040) based raw NAND tool. Its current scope is a **read-only** dumper: the Pico bit-bangs a
Spansion/SkyHigh S34ML02G1 (2 Gb SLC, ×8, BGA63) and streams full raw pages (data + OOB) over USB to a Python host
tool. The host tool verifies, retries, compares and reconciles multi-pass dumps.

- Requirements: [`docs/SPEC.md`](docs/SPEC.md)
- Wire protocol: [`docs/PROTOCOL.md`](docs/PROTOCOL.md)
- Design record (approved): [`docs/PROPOSAL.md`](docs/PROPOSAL.md)

In its current scope the firmware never programs or erases the chip. Only Reset, Read ID, Read Parameter Page, Page
Read and Read Status can reach the bus, and WP# is hard-wired to GND.

## Status LED

The Pico's on-board LED (GP25) shows what the firmware is doing:

| LED | Meaning |
|---|---|
| slow blink, 1 Hz | powered, but USB not enumerated (or suspended) |
| short blip every 2 s | enumerated, no host program has the port open |
| solid on | host program connected (port open), idle |
| flickering | activity: requests / pages moving |
| fast blink, 10 Hz, for 2 s | an error was reported to the host (request CRC error, R/B# timeout) |
| triple blink, repeating | **FAULT**: firmware halted, NAND bus parked (CE# high). Power-cycle the Pico. |

**Optional disk-activity LED:** set `PIN_LED_ACTIVITY` in `firmware/src/pins.h` to a free GPIO (e.g. GP15 →
330 Ω → LED → GND) and rebuild. That LED then lights while requests or pages move, like a disk LED, and the on-board
LED stops flickering and only shows status. A pin that clashes with a NAND line fails to compile.
