"""download(): orchestration, skipping, verification, concurrency, resume end-to-end."""

from __future__ import annotations

from .conftest import FakeDrive
from .conftest import write_part
from collections import deque
from gdrive_fetch.client import DriveClient
from gdrive_fetch.downloader import _dedupe
from gdrive_fetch.downloader import Action
from gdrive_fetch.downloader import download
from gdrive_fetch.downloader import FilePlan
from gdrive_fetch.downloader import plan
from gdrive_fetch.models import DriveFile
from pathlib import Path
from pathlib import PurePosixPath

import hashlib
import pytest
import threading
import time


DATA = bytes(range(256)) * 4


def _f(id_: str, name: str, mime: str = "text/plain") -> DriveFile:
    return DriveFile(id_, name, mime, 1, None, PurePosixPath("sub") / name)


def test_dedupe_appends_id_on_collision() -> None:
    a, b, c = (
        _f("aaaaaaaa1", "x.txt"),
        _f("bbbbbbbb2", "x.txt"),
        _f("cccccccc3", "y.txt"),
    )
    out = _dedupe([a, b, c])
    assert out[c] == Path("sub/y.txt")
    assert out[a] == Path("sub/x__aaaaaaaa.txt")
    assert out[b] == Path("sub/x__bbbbbbbb.txt")


def test_google_doc_gets_export_extension() -> None:
    doc = _f("d", "Notes", "application/vnd.google-apps.document")
    assert doc.local_name == "Notes.docx"
    assert doc.export is not None


async def test_mirrors_folder_tree(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    sub = drive.folder("sub", root)
    drive.file("a.bin", b"aaa", root)
    drive.file("b.bin", b"bbb", sub)
    drive.gdoc("Doc", sub, exported=b"<docx>")

    report = await download(client, root, tmp_path, concurrency=2, quiet=True)

    assert report.ok
    assert sorted(p.relative_to(tmp_path).as_posix() for p in report.downloaded) == [
        "a.bin",
        "sub/Doc.docx",
        "sub/b.bin",
    ]
    assert (tmp_path / "sub" / "b.bin").read_bytes() == b"bbb"


async def test_single_file_id(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("solo.bin", DATA, None)

    report = await download(client, fid, tmp_path, quiet=True)

    assert report.downloaded == [tmp_path / "solo.bin"]
    assert (tmp_path / "solo.bin").read_bytes() == DATA


async def test_second_run_skips_verified_files(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    fid = drive.file("a.bin", DATA, root)

    first = await download(client, root, tmp_path, quiet=True)
    second = await download(client, root, tmp_path, quiet=True)

    assert first.downloaded and not first.skipped
    assert second.skipped == [tmp_path / "a.bin"] and not second.downloaded
    assert len(drive.media_requests(fid)) == 1


async def test_corrupted_local_file_is_redownloaded(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", DATA, root)
    (tmp_path / "a.bin").write_bytes(b"X" * len(DATA))  # same size, wrong content

    report = await download(client, root, tmp_path, quiet=True)

    assert report.downloaded == [tmp_path / "a.bin"]
    assert (tmp_path / "a.bin").read_bytes() == DATA


async def test_md5_mismatch_is_reported_and_file_removed(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    fid = drive.file("a.bin", DATA, root)
    drive.serve_override[fid] = b"Y" * len(
        DATA
    )  # metadata says DATA, wire says otherwise

    report = await download(client, root, tmp_path, quiet=True)

    assert not report.ok
    assert "md5 mismatch" in str(report.failed[tmp_path / "a.bin"])
    assert not (tmp_path / "a.bin").exists()


async def test_interrupted_run_is_continued(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    """End-to-end `gdown --continue` behaviour.

    Run 1 dies mid-transfer and leaves a .part; run 2 picks up where it left off,
    transferring only the missing bytes, and the result passes md5 verification.
    """
    root = drive.folder("root")
    fid = drive.file("big.bin", DATA, root)
    drive.fail_after[fid] = deque(
        [256] + [0] * 10
    )  # run 1: 256 bytes then hard failure

    run1 = await download(client, root, tmp_path, quiet=True)
    part = tmp_path / "big.bin.part"
    assert not run1.ok and part.exists() and not (tmp_path / "big.bin").exists()
    assert part.stat().st_size == 256

    drive.fail_after.pop(fid)
    drive.requests.clear()
    run2 = await download(client, root, tmp_path, quiet=True)

    assert run2.ok and run2.downloaded == [tmp_path / "big.bin"]
    assert (tmp_path / "big.bin").read_bytes() == DATA
    assert not part.exists()
    [req] = drive.media_requests(fid)
    assert req.headers["Range"] == "bytes=256-"


async def test_no_resume_ignores_part(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    fid = drive.file("big.bin", DATA, root)
    write_part(tmp_path / "big.bin", DATA[:100])

    report = await download(client, root, tmp_path, resume=False, quiet=True)

    assert report.ok
    assert "Range" not in drive.media_requests(fid)[0].headers


async def test_concurrency_is_bounded(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    drive.media_delay = 0.02
    root = drive.folder("root")
    for i in range(8):
        drive.file(f"f{i}.bin", b"x" * 8, root)

    await download(client, root, tmp_path, concurrency=2, quiet=True)
    assert drive.max_inflight <= 2

    drive.max_inflight = 0
    await download(client, root, tmp_path / "again", concurrency=8, quiet=True)
    assert drive.max_inflight >= 4  # parallelism actually happens


async def test_failures_do_not_abort_other_files(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    bad = drive.file("bad.bin", DATA, root)
    drive.file("good.bin", b"ok", root)
    drive.fail_after[bad] = deque([0] * 10)

    report = await download(client, root, tmp_path, quiet=True)

    assert report.downloaded == [tmp_path / "good.bin"]
    assert set(report.failed) == {tmp_path / "bad.bin"}


# ------------------------------------------------------------------ dry run


async def _plan_by_name(
    client: DriveClient, root: str, out: Path
) -> dict[str, FilePlan]:
    """The plan for `root`, keyed by the local file name each entry targets."""
    result = await plan(client, root, out)
    return {e.dest.name: e for e in result.entries}


async def test_plan_reports_every_file_as_missing(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", b"aaa", root)
    sub = drive.folder("sub", root)
    drive.file("b.bin", b"bbbb", sub)

    result = await plan(client, root, tmp_path)

    assert [e.action for e in result.entries] == [Action.DOWNLOAD] * 2
    assert {e.reason for e in result.entries} == {"missing locally"}
    assert result.transfer_bytes == 7  # 3 + 4
    assert sorted(e.dest.relative_to(tmp_path).as_posix() for e in result.entries) == [
        "a.bin",
        "sub/b.bin",
    ]


async def test_plan_skips_what_already_matches(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", DATA, root)
    (tmp_path / "a.bin").write_bytes(DATA)

    [entry] = (await plan(client, root, tmp_path)).entries

    assert entry.action is Action.SKIP
    assert entry.reason == "size and md5 match"
    assert entry.transfer_bytes == 0


async def test_plan_flags_a_size_mismatch(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", DATA, root)
    (tmp_path / "a.bin").write_bytes(b"short")

    [entry] = (await plan(client, root, tmp_path)).entries

    assert entry.action is Action.DOWNLOAD
    assert entry.reason == f"size differs (local 5, remote {len(DATA)})"
    assert entry.transfer_bytes == len(DATA)


async def test_plan_flags_a_checksum_mismatch(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", DATA, root)
    (tmp_path / "a.bin").write_bytes(b"X" * len(DATA))  # right size, wrong bytes

    [entry] = (await plan(client, root, tmp_path)).entries

    assert entry.action is Action.DOWNLOAD
    assert entry.reason == "md5 differs"


async def test_plan_trusts_size_alone_without_verify(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", DATA, root)
    (tmp_path / "a.bin").write_bytes(b"X" * len(DATA))

    [entry] = (await plan(client, root, tmp_path, verify=False)).entries

    assert entry.action is Action.SKIP
    assert entry.reason == "size matches"


async def test_plan_counts_only_the_missing_bytes_of_a_partial(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("big.bin", DATA, root)
    write_part(tmp_path / "big.bin", DATA[:300])

    [entry] = (await plan(client, root, tmp_path)).entries

    assert entry.action is Action.RESUME
    assert entry.resume_from == 300
    assert entry.reason == "300 bytes already in .part"
    assert entry.transfer_bytes == len(DATA) - 300


async def test_plan_ignores_a_partial_when_resume_is_off(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("big.bin", DATA, root)
    write_part(tmp_path / "big.bin", DATA[:300])

    [entry] = (await plan(client, root, tmp_path, resume=False)).entries

    assert entry.action is Action.DOWNLOAD
    assert entry.transfer_bytes == len(DATA)


async def test_plan_overwrite_re_fetches_a_matching_file(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", DATA, root)
    (tmp_path / "a.bin").write_bytes(DATA)

    [entry] = (await plan(client, root, tmp_path, skip_existing=False)).entries

    assert entry.action is Action.DOWNLOAD
    assert entry.reason == "size and md5 match, but --overwrite given"


async def test_plan_reports_exports_with_unknown_size(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.gdoc("Notes", root)

    result = await plan(client, root, tmp_path)
    [entry] = result.entries

    assert entry.dest.name == "Notes.docx"
    assert entry.action is Action.DOWNLOAD
    assert entry.transfer_bytes is None
    assert result.unknown_sizes == 1
    assert result.transfer_bytes == 0


async def test_plan_touches_nothing_on_disk(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    """A dry run must not write, delete or transfer anything.

    The oversized .part is the sharp case: a real run deletes it (it cannot be
    a prefix of the file), so the plan has to reach the same verdict through
    the read-only half of that check.
    """
    root = drive.folder("root")
    fid = drive.file("big.bin", DATA, root)
    oversized = write_part(tmp_path / "big.bin", DATA + b"extra")
    before = sorted(p.name for p in tmp_path.iterdir())

    [entry] = (await plan(client, root, tmp_path)).entries

    assert entry.action is Action.DOWNLOAD  # the partial is unusable
    assert entry.resume_from == 0
    assert oversized.exists()  # ...but a dry run leaves it alone
    assert oversized.stat().st_size == len(DATA) + 5
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert drive.media_requests(fid) == []  # metadata only, no transfers


async def test_plan_handles_a_single_file_target(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("solo.bin", DATA, None)

    [entry] = (await plan(client, fid, tmp_path)).entries

    assert entry.dest == tmp_path / "solo.bin"
    assert entry.action is Action.DOWNLOAD
    assert drive.media_requests(fid) == []


async def test_plan_agrees_with_what_download_then_does(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    """The plan is only useful if a real run makes the same choices."""
    root = drive.folder("root")
    drive.file("fresh.bin", b"new", root)
    drive.file("done.bin", DATA, root)
    drive.file("partial.bin", DATA, root)
    (tmp_path / "done.bin").write_bytes(DATA)
    write_part(tmp_path / "partial.bin", DATA[:100])

    before = await _plan_by_name(client, root, tmp_path)
    report = await download(client, root, tmp_path, quiet=True)

    assert before["fresh.bin"].action is Action.DOWNLOAD
    assert before["done.bin"].action is Action.SKIP
    assert before["partial.bin"].action is Action.RESUME
    assert report.skipped == [tmp_path / "done.bin"]
    assert sorted(p.name for p in report.downloaded) == ["fresh.bin", "partial.bin"]


async def test_plan_reports_listing_and_comparison_progress(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", b"aaa", root)
    sub = drive.folder("sub", root)
    drive.file("b.bin", b"bbbb", sub)
    drive.file("c.bin", b"ccccc", sub)

    listed: list[int] = []
    compared: list[tuple[int, int]] = []

    await plan(
        client,
        root,
        tmp_path,
        on_listed=listed.append,
        on_compared=lambda done, total: compared.append((done, total)),
    )

    assert listed == [1, 2, 3]  # a running count, one call per file found
    assert compared == [(1, 3), (2, 3), (3, 3)]  # done/total, once per file


async def test_plan_reports_progress_for_a_single_file_target(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("solo.bin", b"x", None)

    listed: list[int] = []
    compared: list[tuple[int, int]] = []

    await plan(
        client,
        fid,
        tmp_path,
        on_listed=listed.append,
        on_compared=lambda done, total: compared.append((done, total)),
    )

    assert listed == [1]
    assert compared == [(1, 1)]


async def test_download_reports_listing_progress(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", b"aaa", root)
    drive.file("b.bin", b"bbb", root)

    listed: list[int] = []
    report = await download(client, root, tmp_path, quiet=True, on_listed=listed.append)

    assert report.ok
    assert listed == [1, 2]


async def test_plan_is_ordered_by_path(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    """Rows come out alphabetically, not in Drive's arbitrary listing order."""
    root = drive.folder("root")
    zebra = drive.folder("zebra", root)
    alpha = drive.folder("alpha", root)
    for name in ("m.bin", "B.bin", "a.bin"):
        drive.file(name, b"x", root)
    drive.file("z.bin", b"x", zebra)
    drive.file("y.bin", b"x", alpha)

    result = await plan(client, root, tmp_path)

    assert [e.dest.relative_to(tmp_path).as_posix() for e in result.entries] == [
        "a.bin",
        "alpha/y.bin",
        "B.bin",
        "m.bin",
        "zebra/z.bin",
    ]


async def test_plan_ordering_is_case_insensitive_but_total(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    """`a` sorts next to `A`, and files differing only in case keep a stable order."""
    root = drive.folder("root")
    for name in ("b.bin", "A.bin", "a.bin"):
        drive.file(name, b"x", root)

    once = await plan(client, root, tmp_path)
    twice = await plan(client, root, tmp_path)

    names = [e.dest.name for e in once.entries]
    assert names == ["A.bin", "a.bin", "b.bin"]
    assert names == [e.dest.name for e in twice.entries]


async def test_plan_comparisons_are_bounded_by_concurrency(
    drive: FakeDrive,
    client: DriveClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry run honours `-j`: hashing is the slow half, so it must fan out."""
    root = drive.folder("root")
    for i in range(8):
        drive.file(f"f{i}.bin", DATA, root)
        (tmp_path / f"f{i}.bin").write_bytes(DATA)

    lock = threading.Lock()
    inflight = 0
    peak = 0

    def slow_md5(path: Path) -> str:
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.02)
        with lock:
            inflight -= 1
        return hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324

    monkeypatch.setattr("gdrive_fetch.downloader._md5", slow_md5)

    await plan(client, root, tmp_path, concurrency=2)
    assert peak <= 2

    peak = 0
    await plan(client, root, tmp_path, concurrency=8)
    assert peak >= 4  # parallelism actually happens


async def test_plan_survives_comparisons_finishing_out_of_order(
    drive: FakeDrive,
    client: DriveClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Progress counts completions, and rows stay in path order regardless."""
    # `a.bin` sorts first but hashes slowest, so it finishes last.
    delays = {"a.bin": 0.06, "b.bin": 0.03, "c.bin": 0.0}
    root = drive.folder("root")
    for name in delays:
        drive.file(name, DATA, root)
        (tmp_path / name).write_bytes(DATA)

    def staggered_md5(path: Path) -> str:
        time.sleep(delays[path.name])
        return hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324

    monkeypatch.setattr("gdrive_fetch.downloader._md5", staggered_md5)

    compared: list[tuple[int, int]] = []
    result = await plan(
        client,
        root,
        tmp_path,
        concurrency=3,
        on_compared=lambda done, total: compared.append((done, total)),
    )

    assert compared == [(1, 3), (2, 3), (3, 3)]
    assert [e.dest.name for e in result.entries] == ["a.bin", "b.bin", "c.bin"]
