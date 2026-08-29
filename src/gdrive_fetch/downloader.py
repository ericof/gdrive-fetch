from __future__ import annotations

from .client import DriveClient
from .models import DriveFile
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
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


async def _already_present(file: DriveFile, dest: Path, verify: bool) -> bool:
    if not dest.exists():
        return False
    if file.export:
        return True  # no size/md5 for exports; existence is the best we can do
    if file.size is not None and dest.stat().st_size != file.size:
        return False
    if verify and file.md5:
        return await asyncio.to_thread(_md5, dest) == file.md5
    return True


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
    files, is_folder = await collect(client, file_id)
    if not is_folder:
        f = files[0]
        targets = {f: Path(f.local_name)}
    else:
        targets = _dedupe(files)

    report = DownloadReport()
    sem = asyncio.Semaphore(max(1, concurrency))
    total_bytes = sum(f.size or 0 for f in files)

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
