"""Write mode on the host: block erase and page program with optional readback verification, and the `write IMAGE`
job (docs/WRITE_PROPOSAL.md §5-6, docs/PROTOCOL.md "Write mode").

Rules (docs/CLAUDE.md "Hard safety rules"):
- The device is armed for exactly one block at a time, and disarmed after it.
- An erase or program whose response is lost is never re-sent: the whole block is re-done (erase, program, and
  verify if asked).
- With verify=True every written block is read back and compared with what was meant to be written, and a mismatch
  stops the job. Without it, only the chip's status (SR pass/fail) is checked (docs/SPEC.md: readback on request).
- The target's bad-block markers are read before the first erase and kept in the sidecar (§9.2: an erase can destroy
  them). Blocks marked bad are never erased.
- Nothing is marked, remapped or skipped silently: every block's outcome is reported.

How a `write IMAGE` job is built (docs/LEARNING_GUIDE.md §8):

    Image.open()   the file is whole blocks of 64 x 2112 bytes; its chip position comes from --start or its dump sidecar
        │
    build_plan()   read-only pass over the chip: bad-block markers of every target block, and (only-changed mode,
        │          the default) the whole block, compared with the image. One Entry per image block:
        │            write | same | image-bad | target-bad | conflict
        │          --dry-run prints the plan and stops here: nothing has been erased yet.
        ▼
    run_plan()     for each "write" entry: record it as in progress in IMAGE.write.json, then write_block()
                   (arm -> erase -> program pipelined -> disarm, + readback with --verify), then record it as done.

Why erase and then program: NAND programming can only turn 1 bits into 0 bits, and only an erase turns them back to
1 (a whole block at a time). So rewriting even one byte means erasing its block and programming all 64 pages again.
Pages that are all FFh are skipped: an erased page already reads FFh.

Bad-block maps: --map 1:1 puts image block N on chip block N, and refuses if a bad chip block would have to hold
data. --map skip-bad slides the image past bad chip blocks, as many bootloaders and UBI expect.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .client import Client, LostResponse, WriteResult
from .dump import load_meta
from .errors import NandToolError
from .geometry import EXPECTED_ID, PAGE_DATA, PAGE_SIZE, PAGES_PER_BLOCK, TOTAL_PAGES

BLOCKS = TOTAL_PAGES // PAGES_PER_BLOCK
ERASED_PAGE = b"\xff" * PAGE_SIZE
MARKER_PAGES = (0, 1, PAGES_PER_BLOCK - 1)  # §9.2, Fig. 55 note 84
ARM_IDLE_S = 10  # per block: erase + 64 programs take well under 1 s
BLOCK_ATTEMPTS = 3  # a block is re-done after a lost response at most this often
READ_RETRIES = 3  # re-reads of a page that reported an R/B# timeout
SIDECAR_FORMAT = "pico-nand-tool write v1"

MAP_1TO1 = "1:1"
MAP_SKIP_BAD = "skip-bad"


class WriteError(NandToolError):
    pass


def _hex(b: bytes) -> str:
    return b.hex(" ").upper()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- reading ------------------------------------------------------------------------------------------------


def read_range(client: Client, start: int, count: int) -> list[bytes]:
    """Pages [start, start + count). A page that reports an R/B# timeout is re-read up to READ_RETRIES times."""
    pages: list[bytes | None] = [None] * count
    with contextlib.closing(client.read_pages(start, count)) as stream:
        for p, data in stream:
            pages[p - start] = data
    for i, data in enumerate(pages):
        tries = 0
        while data is None:
            if tries == READ_RETRIES:
                raise WriteError(f"page {start + i}: R/B# timeout on {READ_RETRIES + 1} reads")
            tries += 1
            data = list(client.read_pages(start + i, 1))[0][1]
        pages[i] = data
    return pages  # type: ignore[return-value]


def read_block(client: Client, block: int) -> list[bytes]:
    return read_range(client, block * PAGES_PER_BLOCK, PAGES_PER_BLOCK)


def markers_of(pages: list[bytes]) -> dict[int, int]:
    """Non-FFh bad-block markers (page in block -> spare byte 0) of one block's 64 pages."""
    return {p: pages[p][PAGE_DATA] for p in MARKER_PAGES if pages[p][PAGE_DATA] != 0xFF}


def read_markers(client: Client, block: int) -> dict[int, int]:
    """The block's bad-block markers, from the three marker pages only."""
    out = {}
    for p in MARKER_PAGES:
        v = read_range(client, block * PAGES_PER_BLOCK + p, 1)[0][PAGE_DATA]
        if v != 0xFF:
            out[p] = v
    return out


# ---- one block ----------------------------------------------------------------------------------------------


def disarm_quietly(client: Client) -> None:
    # Best effort: if the line is broken the device still disarms itself after ARM_IDLE_S or on the DTR drop.
    with contextlib.suppress(NandToolError):
        client.disarm()


def erase_block(client: Client, block: int, *, ignore_bad_marker: bool = False) -> WriteResult:
    """Arm for this block only, erase it, disarm."""
    client.arm_write(block, block, ARM_IDLE_S)
    try:
        return client.erase_block(block, ignore_bad_marker=ignore_bad_marker)
    finally:
        disarm_quietly(client)


def verify_block(client: Client, block: int, want: list[bytes]) -> None:
    """Read the block back; pages that differ are read once more before the job stops."""
    got = read_block(client, block)
    bad = [i for i in range(PAGES_PER_BLOCK) if got[i] != want[i]]
    first = block * PAGES_PER_BLOCK
    still = [i for i in bad if read_range(client, first + i, 1)[0] != want[i]]
    if still:
        i = still[0]
        again = read_range(client, first + i, 1)[0]
        offs = [o for o in range(PAGE_SIZE) if again[o] != want[i][o]]
        detail = ", ".join(f"offset {o}: {again[o]:02X}h (want {want[i][o]:02X}h)" for o in offs[:4])
        raise WriteError(
            f"block {block}: verify FAILED on {len(still)} page(s) ({', '.join(str(first + j) for j in still[:8])}"
            f"{' ...' if len(still) > 8 else ''}); page {first + i}: {len(offs)} bytes differ: {detail}"
        )


def write_block(
    client: Client, block: int, pages: list[bytes], *, ignore_bad_marker: bool = False, verify: bool = False
) -> int:
    """Erase `block`, program its pages that are not all FFh and, with verify=True, read back and compare. Returns
    pages programmed.

    A lost response re-does the whole block (at most BLOCK_ATTEMPTS times); device errors (bad block, op failed, WP#
    stuck) propagate at once. The device is armed for this block only and disarmed afterwards."""
    if len(pages) != PAGES_PER_BLOCK or any(len(p) != PAGE_SIZE for p in pages):
        raise ValueError("a block is 64 pages of 2112 bytes")
    first = block * PAGES_PER_BLOCK
    # Re-doing the whole block is always safe: whatever a lost erase/program did or did not do, erasing again and
    # programming the pages again ends in the same state. try/finally guarantees the disarm on every exit path.
    for attempt in range(1, BLOCK_ATTEMPTS + 1):
        try:
            client.arm_write(block, block, ARM_IDLE_S)
            try:
                client.erase_block(block, ignore_bad_marker=ignore_bad_marker)
                # An erased page already reads FFh: don't spend a program on it. Pipelined (Client.program_pages).
                todo = [(first + i, data) for i, data in enumerate(pages) if data != ERASED_PAGE]
                client.program_pages(todo)
                programmed = len(todo)
            finally:
                disarm_quietly(client)
        except LostResponse as e:
            if attempt == BLOCK_ATTEMPTS:
                raise WriteError(f"block {block}: response lost on {attempt} attempts, giving up: {e}") from e
            continue
        if verify:
            verify_block(client, block, pages)
        return programmed
    raise AssertionError("unreachable")


# ---- image ----------------------------------------------------------------------------------------------------


@dataclass
class Image:
    path: Path
    first_block: int  # chip block of the image's first block
    blocks: int

    @classmethod
    def open(cls, path: Path, start_page: int | None = None) -> Image:
        if not path.is_file():
            raise WriteError(f"{path}: no such file")
        size = path.stat().st_size
        if size == 0 or size % (PAGE_SIZE * PAGES_PER_BLOCK):
            raise WriteError(f"{path}: {size} bytes is not a whole number of blocks ({PAGE_SIZE * PAGES_PER_BLOCK} B)")
        if start_page is None:
            meta = load_meta(path)
            start_page = meta["start"] if meta else 0
        if start_page % PAGES_PER_BLOCK:
            raise WriteError(f"{path}: starts at page {start_page}, which is not a block boundary")
        img = cls(path, start_page // PAGES_PER_BLOCK, size // (PAGE_SIZE * PAGES_PER_BLOCK))
        if img.first_block + img.blocks > BLOCKS:
            raise WriteError(f"{path}: blocks {img.first_block}..{img.first_block + img.blocks - 1} run past the chip")
        return img

    def block(self, chip_block: int) -> list[bytes]:
        i = chip_block - self.first_block
        if not 0 <= i < self.blocks:
            raise IndexError(chip_block)
        with self.path.open("rb") as f:
            f.seek(i * PAGES_PER_BLOCK * PAGE_SIZE)
            buf = f.read(PAGES_PER_BLOCK * PAGE_SIZE)
        return [buf[o : o + PAGE_SIZE] for o in range(0, len(buf), PAGE_SIZE)]


# ---- sidecar ----------------------------------------------------------------------------------------------------


def sidecar_path(image: Path) -> Path:
    return image.with_name(image.name + ".write.json")


def load_sidecar(image: Path) -> dict | None:
    p = sidecar_path(image)
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise WriteError(f"{p}: unreadable sidecar ({e})") from e
    if meta.get("format") != SIDECAR_FORMAT:
        raise WriteError(f"{p}: not a {SIDECAR_FORMAT!r} sidecar")
    return meta


def _save_sidecar(image: Path, meta: dict) -> None:
    p = sidecar_path(image)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n")
    os.replace(tmp, p)


# ---- plan -------------------------------------------------------------------------------------------------------


@dataclass
class Entry:
    image_block: int
    target_block: int | None
    action: str  # "write", "same", "image-bad", "target-bad", "conflict"
    pages: int = 0  # pages to program (not all FFh)
    reason: str = ""


@dataclass
class Plan:
    image: Image
    mapping: str
    only_changed: bool
    entries: list[Entry]
    target_bad: dict[int, dict[int, int]]  # target block -> {page in block: marker}
    image_bad: dict[int, dict[int, int]]  # image (chip) block -> markers
    resumed_block: int | None = None  # in progress when an earlier run stopped: always re-written
    scanned_blocks: int = 0

    def by_action(self, action: str) -> list[Entry]:
        return [e for e in self.entries if e.action == action]

    @property
    def to_write(self) -> list[Entry]:
        return self.by_action("write")

    @property
    def conflicts(self) -> list[Entry]:
        return self.by_action("conflict")


def build_plan(
    client: Client,
    image: Image,
    *,
    first_block: int | None = None,
    count: int | None = None,
    mapping: str = MAP_1TO1,
    only_changed: bool = True,
    fresh: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> Plan:
    """Decide what to do with each image block in [first_block, first_block + count) (chip numbering of the image).

    Reads the target's bad-block markers (and, in only-changed mode, each whole target block) before anything is
    erased. Markers saved by an unfinished earlier run of this image are merged in unless fresh=True."""
    if mapping not in (MAP_1TO1, MAP_SKIP_BAD):
        raise ValueError(mapping)
    first = image.first_block if first_block is None else first_block
    n = image.first_block + image.blocks - first if count is None else count
    if not (image.first_block <= first and n >= 1 and first + n <= image.first_block + image.blocks):
        raise WriteError(
            f"blocks {first}..{first + n - 1} are not all in the image "
            f"(blocks {image.first_block}..{image.first_block + image.blocks - 1})"
        )
    idb = client.read_id()
    if idb != EXPECTED_ID:
        raise WriteError(f"READ_ID is {_hex(idb)}, expected {_hex(EXPECTED_ID)}: refusing to write (check the socket)")

    saved = load_sidecar(image.path)
    target_bad: dict[int, dict[int, int]] = {}
    resumed_block = None
    if saved and not saved.get("complete") and not fresh:
        target_bad = {int(b): {int(p): v for p, v in m.items()} for b, m in saved.get("target_bad", {}).items()}
        resumed_block = saved.get("in_progress")

    image_bad: dict[int, dict[int, int]] = {}
    # Only the most recently read target block is kept (a whole chip would be 277 MB): each block is compared right
    # after it is read, in both maps, since target blocks are visited in increasing order.
    last_read: tuple[int, list[bytes]] | None = None
    scanned: set[int] = set()

    def target_markers(tb: int) -> dict[int, int]:
        nonlocal last_read
        if tb not in scanned:
            if only_changed:
                last_read = (tb, read_block(client, tb))
                m = markers_of(last_read[1])
            else:
                m = read_markers(client, tb)
            if m:
                target_bad[tb] = {**m, **target_bad.get(tb, {})}
            scanned.add(tb)
        return target_bad.get(tb, {})

    entries: list[Entry] = []
    tb_next = first
    for k, ib in enumerate(range(first, first + n)):
        if progress:
            progress(k, n)
        pages = image.block(ib)
        im = markers_of(pages)
        if im:
            image_bad[ib] = im
        programs = sum(1 for p in pages if p != ERASED_PAGE)

        if mapping == MAP_1TO1:
            tb = ib
            target_markers(tb)
        else:
            if im:  # dropped from the image; no target block used
                entries.append(Entry(ib, None, "image-bad", reason="marked bad in the image: dropped"))
                continue
            while tb_next < BLOCKS and target_markers(tb_next):
                tb_next += 1
            if tb_next >= BLOCKS:
                raise WriteError(f"image block {ib} does not fit: no good target block left after skipping bad ones")
            tb = tb_next
            tb_next += 1

        if im:
            entries.append(Entry(ib, tb, "image-bad", reason="marked bad in the image: target left untouched"))
        elif tb in target_bad:
            if programs:
                entries.append(Entry(ib, tb, "conflict", programs, "target block is bad but the image block has data"))
            else:
                entries.append(Entry(ib, tb, "target-bad", reason="target block is bad; image block is blank"))
        elif only_changed and tb != resumed_block and last_read is not None and last_read == (tb, pages):
            entries.append(Entry(ib, tb, "same"))
        else:
            reason = "interrupted by an earlier run" if tb == resumed_block else ""
            entries.append(Entry(ib, tb, "write", programs, reason))
    if progress:
        progress(n, n)
    return Plan(image, mapping, only_changed, entries, target_bad, image_bad, resumed_block, len(scanned))


# ---- backup check -----------------------------------------------------------------------------------------------


@dataclass
class BackupCheck:
    sampled: list[int] = field(default_factory=list)
    mismatched: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.sampled) and not self.mismatched


def check_backup(client: Client, backup: Path, samples: int = 64, seed: int | None = None) -> BackupCheck:
    """Compare random non-blank pages of the backup with the chip, to show the backup belongs to this chip."""
    if not backup.is_file():
        raise WriteError(f"{backup}: no such file")
    meta = load_meta(backup)
    if meta and meta.get("chip_id") != _hex(EXPECTED_ID):
        raise WriteError(f"{backup}: sidecar says chip ID {meta.get('chip_id')}, not {_hex(EXPECTED_ID)}")
    start = meta["start"] if meta else 0
    n = backup.stat().st_size // PAGE_SIZE
    rng = random.Random(seed)
    result = BackupCheck()
    with backup.open("rb") as f:
        for _ in range(samples * 8):  # skip blank pages; bounded so an all-FFh backup ends
            if len(result.sampled) == samples or n == 0:
                break
            i = rng.randrange(n)
            f.seek(i * PAGE_SIZE)
            want = f.read(PAGE_SIZE)
            if want == ERASED_PAGE or start + i in result.sampled:
                continue
            result.sampled.append(start + i)
            if read_range(client, start + i, 1)[0] != want:
                result.mismatched.append(start + i)
    return result


# ---- run --------------------------------------------------------------------------------------------------------


@dataclass
class BlockDone:
    entry: Entry
    programmed: int
    index: int  # 1-based among blocks to write
    total: int


def run_plan(
    client: Client, plan: Plan, *, verify: bool = False, progress: Callable[[BlockDone], None] | None = None
) -> int:
    """Write every "write" entry in order (read back and compared when verify=True); returns the number of blocks
    written. Saves the sidecar (target markers first, then the block in progress) before each erase, so an
    interrupted run re-does that block. Raises on conflicts and on the first failure."""
    if plan.conflicts:
        raise WriteError(f"{len(plan.conflicts)} target bad block(s) would receive image data; nothing was written")
    info = client.ping()
    meta = {
        "format": SIDECAR_FORMAT,
        "image": plan.image.path.name,
        "chip_id": _hex(EXPECTED_ID),
        "mapping": plan.mapping,
        "only_changed": plan.only_changed,
        "verify": verify,
        "target_bad": {str(b): {str(p): v for p, v in m.items()} for b, m in sorted(plan.target_bad.items())},
        "in_progress": plan.resumed_block,
        "blocks_written": [],
        "complete": False,
        "started": _now(),
        "ended": None,
        "result": "running",
        "host_tool": __version__,
        "firmware": info.version_string,
    }
    _save_sidecar(plan.image.path, meta)  # the target's markers are on disk before the first erase (§9.2)
    todo = plan.to_write
    try:
        # The sidecar is written before and after each block. If the run dies in between, "in_progress" names the
        # block whose state is unknown, and the next run's build_plan() always re-writes it.
        for k, e in enumerate(todo, 1):
            assert e.target_block is not None
            meta["in_progress"] = e.target_block
            _save_sidecar(plan.image.path, meta)
            programmed = write_block(client, e.target_block, plan.image.block(e.image_block), verify=verify)
            meta["blocks_written"].append(e.target_block)
            meta["in_progress"] = None
            _save_sidecar(plan.image.path, meta)
            if progress:
                progress(BlockDone(e, programmed, k, len(todo)))
    except BaseException as ex:
        meta["ended"] = _now()
        meta["result"] = "interrupted" if isinstance(ex, KeyboardInterrupt) else f"error: {ex}"
        _save_sidecar(plan.image.path, meta)
        raise
    meta["ended"] = _now()
    meta["result"] = "complete"
    meta["complete"] = True
    _save_sidecar(plan.image.path, meta)
    return len(todo)
