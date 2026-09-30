# pico_nand_tool

A Raspberry Pi Pico (RP2040) based raw NAND tool. Its current scope is a **read-only** dumper: the Pico bit-bangs a
Spansion/SkyHigh S34ML02G1 (2 Gb SLC, ×8, BGA63) and streams full raw pages (data + OOB) over USB to a Python host
tool. The host tool verifies, retries, compares and reconciles multi-pass dumps.

- Requirements: [`docs/SPEC.md`](docs/SPEC.md)
- Wire protocol: [`docs/PROTOCOL.md`](docs/PROTOCOL.md)
- Design record (approved): [`docs/PROPOSAL.md`](docs/PROPOSAL.md)

In its current scope the firmware never programs or erases the chip. Only Reset, Read ID, Read Parameter Page, Page
Read and Read Status can reach the bus, and WP# is hard-wired to GND.
