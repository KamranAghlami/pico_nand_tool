"""Raw dump: pages in order, 2112 bytes each, with a JSON sidecar, retries and resume (docs/SPEC.md `dump`,
docs/PROPOSAL.md §5).

The dump is the only copy of the data, so: an existing file is never overwritten without force, a page is never
zero-filled or guessed (a page that keeps failing stops the dump, which can then be resumed), and everything written
is flushed and fsynced chunk by chunk.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .client import Client
from .errors import NandToolError
from .geometry import EXPECTED_ID, PAGE_DATA, PAGE_SIZE, PAGE_SPARE, TOTAL_PAGES

META_FORMAT = "pico-nand-tool raw dump v1"
DEFAULT_CHUNK = 1024  # pages per READ_PAGES stream; READ_ID is re-checked between chunks
DEFAULT_RETRIES = 5  # re-reads of a page that reported an R/B# timeout


class DumpError(NandToolError):
    pass


def meta_path(out: Path) -> Path:
    return out.with_name(out.name + ".meta.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hex(b: bytes) -> str:
    return b.hex(" ").upper()


def load_meta(out: Path) -> dict | None:
    p = meta_path(out)
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise DumpError(f"{p}: unreadable sidecar ({e})") from e
    if meta.get("format") != META_FORMAT:
        raise DumpError(f"{p}: not a {META_FORMAT!r} sidecar")
    return meta


def _write_meta(out: Path, meta: dict) -> None:
    """Atomic: a crash leaves either the old or the new sidecar, never half of one."""
    p = meta_path(out)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n")
    os.replace(tmp, p)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


@dataclass
class Progress:
    done: int  # pages in the file
    total: int  # pages the dump will have
    session_pages: int  # pages read in this run
    elapsed_s: float  # this run

    @property
    def rate_bps(self) -> float:
        return self.session_pages * PAGE_SIZE / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def eta_s(self) -> float | None:
        rate = self.session_pages / self.elapsed_s if self.elapsed_s > 0 else 0
        return (self.total - self.done) / rate if rate > 0 else None


@dataclass
class DumpResult:
    out: Path
    start: int
    count: int
    resumed_at: int  # pages already in the file when this run started
    pages_read: int  # pages read in this run
    rb_retries: int
    stream_restarts: int
    elapsed_s: float
    sha256: str


def dump(
    client: Client,
    out: Path,
    *,
    start: int = 0,
    count: int | None = None,
    resume: bool = False,
    force: bool = False,
    retries: int = DEFAULT_RETRIES,
    chunk: int = DEFAULT_CHUNK,
    progress: Callable[[Progress], None] | None = None,
) -> DumpResult:
    count = TOTAL_PAGES - start if count is None else count
    if not (0 <= start < TOTAL_PAGES and 1 <= count <= TOTAL_PAGES - start):
        raise DumpError(f"page range {start}+{count} is outside 0..{TOTAL_PAGES - 1}")
    if resume and force:
        raise DumpError("--resume and --force exclude each other")

    meta = load_meta(out) if resume else None
    if resume:
        if not out.exists() or meta is None:
            raise DumpError(f"cannot resume: {out} or its sidecar {meta_path(out).name} is missing")
        want = {"start": start, "count": count, "page_size": PAGE_SIZE, "chip_id": _hex(EXPECTED_ID)}
        have = {k: meta.get(k) for k in want}
        if have != want:
            raise DumpError(f"cannot resume: sidecar says {have}, this run asks for {want}")
    elif not force:
        for p in (out, meta_path(out)):
            if p.exists():
                raise DumpError(f"{p} exists: use --resume to continue it or --force to overwrite it")

    # Talk to the chip before touching any file.
    info = client.ping()
    idb = client.read_id()
    if idb != EXPECTED_ID:
        raise DumpError(f"READ_ID is {_hex(idb)}, expected {_hex(EXPECTED_ID)}: refusing to dump (check the socket)")
    mode, timing = client.get_timing()

    out.parent.mkdir(parents=True, exist_ok=True)
    if resume:
        size = out.stat().st_size
        if size > count * PAGE_SIZE:
            raise DumpError(f"cannot resume: {out} is {size} bytes, more than {count} pages; not touching it")
        done = size // PAGE_SIZE
        f = out.open("r+b")
        f.truncate(done * PAGE_SIZE)  # drop a partial last page
    else:
        done = 0
        f = out.open("wb")
        meta = {
            "format": META_FORMAT,
            "page_size": PAGE_SIZE,
            "data_size": PAGE_DATA,
            "spare_size": PAGE_SPARE,
            "start": start,
            "count": count,
            "chip_id": _hex(EXPECTED_ID),
            "pages_done": 0,
            "complete": False,
            "sha256": None,
            "sessions": [],
        }
    assert meta is not None
    resumed_at = done
    session = {
        "started": _now(),
        "host_tool": __version__,
        "firmware": info.version_string,
        "protocol": info.proto_version,
        "clk_hz": info.clk_hz,
        "timing_mode": mode.name,
        "timing": asdict(timing),
        "first_page": start + done,
        "pages_read": 0,
        "rb_retries": 0,
        "stream_restarts": 0,
        "ended": None,
        "result": "running",
    }
    meta["sessions"].append(session)
    meta["complete"] = False
    meta["sha256"] = None
    restarts0 = client.stream_restarts
    t0 = time.monotonic()

    def save(result: str) -> None:
        meta["pages_done"] = done
        session["ended"] = _now()
        session["result"] = result
        session["stream_restarts"] = client.stream_restarts - restarts0
        _write_meta(out, meta)

    try:
        f.seek(done * PAGE_SIZE)
        save("running")
        while done < count:
            first = start + done
            n = min(chunk, count - done)
            if session["pages_read"]:  # the chip may have lost contact in the socket since the last chunk
                idb = client.read_id()
                if idb != EXPECTED_ID:
                    raise DumpError(f"READ_ID changed to {_hex(idb)} before page {first}: check the socket")
            pages: list[bytes | None] = [None] * n
            with contextlib.closing(client.read_pages(first, n)) as stream:
                for p, data in stream:
                    pages[p - first] = data
            for i, data in enumerate(pages):
                tries = 0
                while data is None:  # R/B# timeout: re-read just this page
                    if tries == retries:
                        f.write(b"".join(pages[:i]))  # type: ignore[arg-type]  # all good so far
                        done += i
                        raise DumpError(
                            f"page {first + i}: R/B# timeout on {retries + 1} reads; stopped there "
                            f"(nothing was filled in). Fix the cause, then use --resume."
                        )
                    tries += 1
                    session["rb_retries"] += 1
                    data = list(client.read_pages(first + i, 1))[0][1]
                pages[i] = data
            f.write(b"".join(pages))  # type: ignore[arg-type]
            f.flush()
            os.fsync(f.fileno())
            done += n
            session["pages_read"] += n
            save("running")
            if progress:
                progress(Progress(done, count, session["pages_read"], time.monotonic() - t0))
    except BaseException as e:
        f.flush()
        os.fsync(f.fileno())
        f.close()
        save("interrupted" if isinstance(e, KeyboardInterrupt) else f"error: {e}")
        raise
    f.close()
    digest = sha256_file(out)
    meta["complete"] = True
    meta["sha256"] = digest
    save("complete")
    return DumpResult(
        out=out,
        start=start,
        count=count,
        resumed_at=resumed_at,
        pages_read=session["pages_read"],
        rb_retries=session["rb_retries"],
        stream_restarts=session["stream_restarts"],
        elapsed_s=time.monotonic() - t0,
        sha256=digest,
    )
