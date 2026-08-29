from __future__ import annotations

from .client import DriveClient
from .client import usable_partial
from .models import DriveFile
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from pathlib import Path
from rich.progress import BarColumn
from rich.progress import DownloadColumn
from rich.progress import Progress
from rich.progress import TaskID
from rich.progress import TextColumn
from rich.progress import TimeRemainingColumn
from rich.progress import TransferSpeedColumn

import asyncio
import hashlib


@dataclass(slots=True)
class DownloadReport:
    downloaded: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: dict[Path, Exception] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


class Action(StrEnum):
    """What a real run would do with one file."""

    DOWNLOAD = "download"
    RESUME = "resume"
    SKIP = "skip"


@dataclass(slots=True, frozen=True)
class FilePlan:
    """One file's verdict: what `download` would do with it, and why."""

    file: DriveFile
    dest: Path
    action: Action
    reason: str
    resume_from: int = 0

    @property
    def transfer_bytes(self) -> int | None:
        """Bytes this entry would pull over the wire.

        :returns: the byte count, or ``None`` when Drive reports no size
            (Google-native exports), so the caller can say "unknown".
        """
        if self.action is Action.SKIP:
            return 0
        if self.file.size is None:
            return None
        return max(0, self.file.size - self.resume_from)


@dataclass(slots=True)
class DownloadPlan:
    """What `download` would do, without having done any of it."""

    entries: list[FilePlan] = field(default_factory=list)

    def of(self, action: Action) -> list[FilePlan]:
        """Entries with a given verdict.

        :param action: the verdict to filter on.
        :returns: the matching entries, in listing order.
        """
        return [e for e in self.entries if e.action is action]

    @property
    def transfer_bytes(self) -> int:
        """Bytes to transfer, counting entries of unknown size as zero."""
        return sum(e.transfer_bytes or 0 for e in self.entries)

    @property
    def unknown_sizes(self) -> int:
        """How many entries would transfer an amount Drive does not report."""
        return sum(1 for e in self.entries if e.transfer_bytes is None)


async def collect(client: DriveClient, file_id: str) -> tuple[list[DriveFile], bool]:
    """Return (files, is_folder). A single-file id yields a one-item list."""
    root = await client.get_file(file_id)
    if not root.is_folder:
        return [root], False
    files = [f async for f in client.walk(file_id)]
    return files, True


def _dedupe(files: list[DriveFile]) -> dict[DriveFile, Path]:
    """Drive allows duplicate names in one folder; disambiguate with the id."""
    seen: dict[Path, int] = {}
    counts: dict[Path, int] = {}
    for f in files:
        p = Path(f.relative_path.parent) / f.local_name
        counts[p] = counts.get(p, 0) + 1
    out: dict[DriveFile, Path] = {}
    for f in files:
        p = Path(f.relative_path.parent) / f.local_name
        if counts[p] > 1:
            seen[p] = seen.get(p, 0) + 1
            p = p.with_name(f"{p.stem}__{f.id[:8]}{p.suffix}")
        out[f] = p
    return out


def _md5(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - Drive's checksum is md5
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _local_state(file: DriveFile, dest: Path, verify: bool) -> tuple[bool, str]:
    """Decide whether `dest` already satisfies `file`, and say why.

    This is the one place that answers the question, so `download` and `plan`
    cannot drift apart in what they consider "already there".

    :param file: the Drive metadata to compare against.
    :param dest: the local path, which need not exist.
    :param verify: also compare md5 when Drive reports one.
    :returns: ``(present, reason)``; `reason` explains either verdict.
    """
    if not dest.exists():
        return False, "missing locally"
    if file.export:
        # No size/md5 for exports; existence is the best we can do.
        return True, "present (export: no size or md5 to check)"
    local = dest.stat().st_size
    if file.size is not None and local != file.size:
        return False, f"size differs (local {local}, remote {file.size})"
    if verify and file.md5:
        if await asyncio.to_thread(_md5, dest) == file.md5:
            return True, "size and md5 match"
        return False, "md5 differs"
    return True, "size matches"


async def _already_present(file: DriveFile, dest: Path, verify: bool) -> bool:
    present, _ = await _local_state(file, dest, verify)
    return present


async def _targets(client: DriveClient, file_id: str) -> dict[DriveFile, Path]:
    """Map every file under `file_id` to its path relative to the output dir.

    :param client: the Drive client to list through.
    :param file_id: id of a folder or of a single file.
    :returns: each file with the relative path it would be written to.
    """
    files, is_folder = await collect(client, file_id)
    if not is_folder:
        return {files[0]: Path(files[0].local_name)}
    return _dedupe(files)


async def plan(
    client: DriveClient,
    file_id: str,
    dest_dir: Path,
    *,
    verify: bool = True,
    skip_existing: bool = True,
    resume: bool = True,
) -> DownloadPlan:
    """Work out what `download` would do, without transferring anything.

    Metadata is read from Drive and compared against what is already in
    `dest_dir`. Nothing is downloaded, written or deleted — in particular an
    unusable ``.part`` file is reported but left on disk, where a real run
    would remove it.

    :param client: the Drive client to read metadata through.
    :param file_id: id of the folder or file to inspect.
    :param dest_dir: the directory files would be mirrored into.
    :param verify: compare md5 as well as size, as `download` would.
    :param skip_existing: report matching files as skipped rather than fetched.
    :param resume: account for ``.part`` files left by an interrupted run.
    :returns: one :class:`FilePlan` per file, in listing order.
    """
    out = DownloadPlan()
    for f, rel in (await _targets(client, file_id)).items():
        dest = dest_dir / rel
        present, reason = await _local_state(f, dest, verify)
        if present and skip_existing:
            out.entries.append(FilePlan(f, dest, Action.SKIP, reason))
            continue
        if present:
            reason = f"{reason}, but --overwrite given"
        part = dest.with_name(dest.name + ".part")
        # Exports ignore Range, so a .part is never resumable for them.
        offset = usable_partial(part, f.size) if resume and not f.export else 0
        if offset:
            out.entries.append(
                FilePlan(
                    f, dest, Action.RESUME, f"{offset} bytes already in .part", offset
                )
            )
        else:
            out.entries.append(FilePlan(f, dest, Action.DOWNLOAD, reason))
    return out


async def download(
    client: DriveClient,
    file_id: str,
    dest_dir: Path,
    *,
    concurrency: int = 4,
    verify: bool = True,
    skip_existing: bool = True,
    resume: bool = True,
    quiet: bool = False,
    on_file_done: Callable[[DriveFile, Path], None] | None = None,
) -> DownloadReport:
    """Download `file_id` (a folder or a single file) into `dest_dir`.

    - Folders are mirrored recursively under `dest_dir`.
    - At most `concurrency` transfers run at once.
    - Existing files whose size/md5 match are skipped when `skip_existing`.
    - md5 is verified after download when `verify` and Drive reports one.
    - Partial `.part` files from an interrupted run are continued when `resume`.
    """
    targets = await _targets(client, file_id)

    report = DownloadReport()
    sem = asyncio.Semaphore(max(1, concurrency))
    total_bytes = sum(f.size or 0 for f in targets)

    progress = Progress(
        TextColumn("[bold blue]{task.fields[name]}", justify="right"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        disable=quiet,
        transient=True,
    )
    overall: TaskID = progress.add_task(
        "total", total=total_bytes or None, name="TOTAL"
    )

    async def one(f: DriveFile, rel: Path) -> None:
        dest = dest_dir / rel
        async with sem:
            if skip_existing and await _already_present(f, dest, verify):
                report.skipped.append(dest)
                progress.advance(overall, f.size or 0)
                return
            task = progress.add_task(str(rel), total=f.size, name=rel.name[:40])

            def tick(n: int) -> None:
                progress.advance(task, n)
                progress.advance(overall, n)

            try:
                await client.download(f, dest, resume=resume, on_progress=tick)
                if verify and f.md5 and not f.export:
                    actual = await asyncio.to_thread(_md5, dest)
                    if actual != f.md5:
                        dest.unlink(missing_ok=True)
                        raise RuntimeError(
                            f"md5 mismatch: expected {f.md5}, got {actual}"
                        )
                report.downloaded.append(dest)
                if on_file_done:
                    on_file_done(f, dest)
            except Exception as exc:
                report.failed[dest] = exc
            finally:
                progress.remove_task(task)

    with progress:
        await asyncio.gather(*(one(f, rel) for f, rel in targets.items()))

    return report
