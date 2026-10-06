"""Host tool for the Pico NAND Tool firmware.

Package map, lowest layer first (docs/LEARNING_GUIDE.md §3, §8):

    errors.py     exception hierarchy; the CLI prints any NandToolError as `error: ...`
    geometry.py   chip facts (page/block sizes, expected ID) and decoders for ID bytes and the status register
    onfi.py       ONFI parameter page: CRC-16 and field decoding
    protocol.py   the wire format: constants, frame encode/decode, CRC-32 (pure functions, no I/O)
    transport.py  moves bytes: pyserial port + auto-detection (tests swap in tests/fake_device.py)
    client.py     one method per device command; seq matching, retries, resync, READ_PAGES streaming, pipelining
    dump.py       `dump`: pages to a file with a JSON sidecar, retries, resume
    compare.py    `compare`: page-by-page diff of two dumps
    reconcile.py  `reconcile`: re-read differing pages, per-bit majority vote
    postproc.py   `split` (data/OOB) and the factory bad-block scan
    write.py      write mode: erase/program a block, plan and run `write IMAGE`
    cli.py        argparse front end: one cmd_xxx() function per subcommand
"""

# The one place the host version lives (pyproject.toml reads it). Release builds overwrite it from the git tag
# (CI: tag v1.2.3 -> "1.2.3"); keep the default equal to the latest tag.
__version__ = "0.2.0"
