# pico_nand_dumper

A read-only raw NAND dumper. A Raspberry Pi Pico (RP2040) bit-bangs a Spansion/SkyHigh S34ML02G1 (2 Gb SLC, ×8,
BGA63) and streams full raw pages (data + OOB) over USB to a Python host tool. The host tool verifies, retries,
compares and reconciles multi-pass dumps.

**Status:** design review. See [`docs/PROPOSAL.md`](docs/PROPOSAL.md). Requirements are in
[`docs/SPEC.md`](docs/SPEC.md).

By design, this tool never programs or erases the chip. Only Reset, Read ID, Read Parameter Page, Page Read and
Read Status can reach the bus, and WP# is hard-wired to GND.
