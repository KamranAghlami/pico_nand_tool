"""Reconcile two dump passes (docs/SPEC.md `reconcile`).

Pages that are identical in A and B are copied as they are. Every page that differs is read again K times from the
device, and each bit takes the majority value of those K fresh reads. The JSON report lists every bit that was not
unanimous across A, B and the re-reads, with its vote counts.

A page is only "corrected" if every bit's majority is clear: at least min_agree of the K reads. Otherwise it is
flagged "unstable". It still gets the majority value (a page can't be left out or zero-filled), but the report and
the exit status say so: unstable pages are flagged, not silently fixed.

Worked example, K = 5 reads, min_agree = 4. For one bit of one byte:
    reads 1 1 1 1 1   -> 1, unanimous                      (reported only if A or B had a 0)
    reads 1 1 1 1 0   -> 1, 4 of 5 agree: "corrected"      (reported with votes_1 = 4, votes_0 = 1)
    reads 1 1 1 0 0   -> 1, only 3 of 5 agree: "unstable"  (page flagged; exit status 1)
A and B (the two original dumps) are not votes. They only decide which pages get re-read, and they appear in the
report for comparison.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path

from .client import Client
from .compare import CompareError, diff_pages, page_count
from .errors import NandToolError
from .geometry import EXPECTED_ID, PAGE_DATA, PAGE_SIZE

DEFAULT_READS = 5


class ReconcileError(NandToolError):
    pass


def default_min_agree(reads: int) -> int:
    return max(ceil(2 * reads / 3), reads // 2 + 1)  # 5 reads -> 4, 3 -> 2, 7 -> 5, 9 -> 6


@dataclass
class PageOutcome:
    page: int  # chip page index
    status: str  # "consistent" (all re-reads identical), "corrected" (clear majority) or "unstable"
    bits: list[dict] = field(default_factory=list)


@dataclass
class ReconcileResult:
    pages: int
    outcomes: list[PageOutcome]

    @property
    def unstable(self) -> list[PageOutcome]:
        return [o for o in self.outcomes if o.status == "unstable"]


def _read_k(client: Client, page: int, k: int, rb_retries: int) -> list[bytes]:
    reads: list[bytes] = []
    timeouts = 0
    while len(reads) < k:
        data = list(client.read_pages(page, 1))[0][1]
        if data is None:
            timeouts += 1
            if timeouts > rb_retries:
                raise ReconcileError(f"page {page}: R/B# timeout on {timeouts} reads")
            continue
        reads.append(data)
    return reads


def vote_page(a: bytes, b: bytes, reads: list[bytes], min_agree: int) -> tuple[bytes, str, list[dict]]:
    """Per-bit majority over the re-reads. Returns (final page, status, report entries)."""
    k = len(reads)
    final = bytearray(reads[0])
    bits: list[dict] = []
    weak = False
    for off in range(len(a)):
        # Every source agrees on this byte (the common case): nothing to vote on. A set of size 1 = all equal.
        column = {a[off], b[off], *(r[off] for r in reads)}
        if len(column) == 1:
            continue
        # Otherwise vote bit by bit: m selects one bit, and the byte is rebuilt from the 8 winning bits.
        byte = 0
        for bit in range(8):
            m = 1 << bit
            ones = sum(1 for r in reads if r[off] & m)
            zeros = k - ones
            value = 1 if ones > zeros else 0
            if ones == zeros or max(ones, zeros) < min_agree:
                weak = True
            byte |= value << bit
            a_bit, b_bit = int(bool(a[off] & m)), int(bool(b[off] & m))
            if ones not in (0, k) or a_bit != value or b_bit != value:
                bits.append(
                    {
                        "offset": off,
                        "area": "data" if off < PAGE_DATA else "spare",
                        "bit": bit,
                        "votes_0": zeros,
                        "votes_1": ones,
                        "a": a_bit,
                        "b": b_bit,
                        "final": value,
                    }
                )
        final[off] = byte
    if weak:
        status = "unstable"
    elif all(r == reads[0] for r in reads):
        status = "consistent"
    else:
        status = "corrected"
    return bytes(final), status, bits


def reconcile(
    client: Client,
    a: Path,
    b: Path,
    out: Path,
    report: Path,
    *,
    start: int = 0,
    reads: int = DEFAULT_READS,
    min_agree: int | None = None,
    force: bool = False,
    rb_retries: int = 5,
) -> ReconcileResult:
    min_agree = default_min_agree(reads) if min_agree is None else min_agree
    if reads < 1 or not (reads // 2 < min_agree <= reads):
        raise ReconcileError(f"min_agree must be a majority of the {reads} reads ({reads // 2 + 1}..{reads})")
    for p in (out, report):
        if p.exists() and not force:
            raise ReconcileError(f"{p} exists: use --force to overwrite it")
    if {out.resolve(), report.resolve()} & {a.resolve(), b.resolve()} or out.resolve() == report.resolve():
        raise ReconcileError("the output and the report must be new files, not one of the inputs or each other")
    try:
        n = page_count(a)
        if page_count(b) != n:
            raise ReconcileError(f"{a} and {b} have different sizes")
    except CompareError as e:
        raise ReconcileError(str(e)) from e

    diffs = list(diff_pages(a, b))
    if diffs:
        idb = client.read_id()
        if idb != EXPECTED_ID:
            raise ReconcileError(f"READ_ID is {idb.hex(' ').upper()}: refusing to re-read (check the socket)")

    tmp = out.with_name(out.name + ".partial")
    shutil.copyfile(a, tmp)
    outcomes: list[PageOutcome] = []
    try:
        with tmp.open("r+b") as f:
            for d in diffs:
                page = start + d.index
                final, status, bits = vote_page(d.a, d.b, _read_k(client, page, reads, rb_retries), min_agree)
                f.seek(d.index * PAGE_SIZE)
                f.write(final)
                outcomes.append(PageOutcome(page, status, bits))
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, out)

    doc = {
        "a": str(a),
        "b": str(b),
        "out": str(out),
        "start_page": start,
        "pages": n,
        "reads_per_page": reads,
        "min_agree": min_agree,
        "pages_differing": len(diffs),
        "pages_unstable": [o.page for o in outcomes if o.status == "unstable"],
        "page_results": [{"page": o.page, "status": o.status, "bits": o.bits} for o in outcomes],
    }
    report.write_text(json.dumps(doc, indent=1) + "\n")
    return ReconcileResult(n, outcomes)
