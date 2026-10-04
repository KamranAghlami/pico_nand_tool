"""Page-by-page comparison of two raw dumps (docs/SPEC.md `compare`)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .errors import NandToolError
from .geometry import PAGE_SIZE

_BATCH = 512  # pages per read: 1 MiB


class CompareError(NandToolError):
    pass


@dataclass(frozen=True)
class PageDiff:
    index: int  # page index within the files (add the dump's start page for the chip page)
    a: bytes
    b: bytes

    @property
    def offsets(self) -> list[int]:
        return [i for i, (x, y) in enumerate(zip(self.a, self.b)) if x != y]

    @property
    def bit_count(self) -> int:
        return sum(bin(x ^ y).count("1") for x, y in zip(self.a, self.b))


def page_count(path: Path) -> int:
    size = path.stat().st_size
    if size % PAGE_SIZE:
        raise CompareError(f"{path}: {size} bytes is not a whole number of {PAGE_SIZE}-byte pages")
    return size // PAGE_SIZE


def diff_pages(a: Path, b: Path) -> Iterator[PageDiff]:
    """Yield every page that differs, over the pages both files have."""
    n = min(page_count(a), page_count(b))
    with a.open("rb") as fa, b.open("rb") as fb:
        for base in range(0, n, _BATCH):
            k = min(_BATCH, n - base)
            ba, bb = fa.read(k * PAGE_SIZE), fb.read(k * PAGE_SIZE)
            if ba == bb:
                continue
            for i in range(k):
                pa, pb = ba[i * PAGE_SIZE : (i + 1) * PAGE_SIZE], bb[i * PAGE_SIZE : (i + 1) * PAGE_SIZE]
                if pa != pb:
                    yield PageDiff(base + i, pa, pb)
