"""Post-processing of a raw image (no device needed): data/OOB split and the factory bad-block scan.

- split: a raw image interleaves each page's 2048 data bytes with its 64 spare (OOB) bytes. Most analysis tools
  (filesystem extractors, binwalk...) want the data alone, and ECC/metadata tools want the OOB alone, so split()
  writes them to two files. Re-interleaving the two gives back the original image.
- bad-block scan: the factory marks a bad block with a non-FFh value in spare byte 0 of page 0, 1 or 63 of the
  block. A used chip complicates this: the previous system may have written its own data there, so the CLI warns when
  the count looks implausible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .compare import CompareError, page_count
from .errors import NandToolError
from .geometry import PAGE_DATA, PAGE_SIZE, PAGES_PER_BLOCK

_BATCH = 512  # pages per read: 1 MiB

# §9.2, Fig. 55 note 84: the 1st byte of the spare area of these pages of a block is FFh in a good block
MARKER_PAGES = (0, 1, PAGES_PER_BLOCK - 1)
MARKER_OFFSET = PAGE_DATA


class PostprocError(NandToolError):
    pass


def _pages(image: Path) -> int:
    try:
        return page_count(image)
    except CompareError as e:
        raise PostprocError(str(e)) from e


def split(image: Path, data_out: Path, oob_out: Path, *, force: bool = False) -> int:
    """Write the 2048 data bytes of every page to data_out and the 64 spare bytes to oob_out. Returns pages."""
    n = _pages(image)
    targets = (data_out, oob_out)
    if len({p.resolve() for p in (image, *targets)}) != 3:
        raise PostprocError("the image, the data file and the OOB file must be three different files")
    for p in targets:
        if p.exists() and not force:
            raise PostprocError(f"{p} exists: use --force to overwrite it")
    tmps = [p.with_name(p.name + ".partial") for p in targets]
    try:
        with image.open("rb") as src, tmps[0].open("wb") as fd, tmps[1].open("wb") as fo:
            for base in range(0, n, _BATCH):
                buf = src.read(min(_BATCH, n - base) * PAGE_SIZE)
                pages = [buf[i : i + PAGE_SIZE] for i in range(0, len(buf), PAGE_SIZE)]
                fd.write(b"".join(p[:PAGE_DATA] for p in pages))
                fo.write(b"".join(p[PAGE_DATA:] for p in pages))
            for f in (fd, fo):
                f.flush()
                os.fsync(f.fileno())
    except BaseException:
        for t in tmps:
            t.unlink(missing_ok=True)
        raise
    for t, p in zip(tmps, targets):
        os.replace(t, p)
    return n


@dataclass(frozen=True)
class BadBlock:
    block: int
    markers: dict[int, int]  # page within the block -> first spare byte, for the marker pages that are not FFh


@dataclass
class BadBlockScan:
    first_block: int
    blocks: int  # whole blocks scanned
    bad: list[BadBlock]
    skipped_pages: int  # pages outside whole blocks (image not block-aligned)


def scan_bad_blocks(image: Path, start_page: int = 0) -> BadBlockScan:
    """Factory bad-block scan (§9.2, Fig. 55): a block is bad if the first spare byte of its 1st, 2nd or last page
    is not FFh. Only whole blocks in the image are scanned."""
    n = _pages(image)
    # A dump need not start on a block boundary. Python's % is never negative, so (-start) % 64 is the number of pages
    # up to the next boundary: start 0 -> 0, start 70 -> 58 (70 + 58 = 128 = block 2).
    lead = (-start_page) % PAGES_PER_BLOCK  # pages before the first block boundary
    blocks = max(0, (n - lead) // PAGES_PER_BLOCK)
    first_block = (start_page + lead) // PAGES_PER_BLOCK
    bad: list[BadBlock] = []
    with image.open("rb") as f:
        for i in range(blocks):
            base = lead + i * PAGES_PER_BLOCK
            markers = {}
            for p in MARKER_PAGES:
                f.seek((base + p) * PAGE_SIZE + MARKER_OFFSET)
                value = f.read(1)[0]
                if value != 0xFF:
                    markers[p] = value
            if markers:
                bad.append(BadBlock(first_block + i, markers))
    return BadBlockScan(first_block, blocks, bad, n - blocks * PAGES_PER_BLOCK)
