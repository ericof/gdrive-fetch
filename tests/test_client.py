"""DriveClient: listing, downloading and — above all — resume semantics."""

from __future__ import annotations

from .conftest import FakeDrive
from .conftest import write_part
from collections import deque
from gdrive_fetch.client import DriveClient
from gdrive_fetch.client import DriveError
from gdrive_fetch.models import DriveFile
from pathlib import Path
from pathlib import PurePosixPath

import httpx
import pytest


DATA = bytes(range(256)) * 4  # 1024 bytes, chunk_size=4 -> many chunks


async def _file(client: DriveClient, fid: str) -> DriveFile:
    return await client.get_file(fid)


# ------------------------------------------------------------------ listing


async def test_walk_recurses_and_resolves_shortcuts(
    drive: FakeDrive, client: DriveClient
) -> None:
    root = drive.folder("root")
    sub = drive.folder("sub", root)
    deep = drive.folder("deep", sub)
    a = drive.file("a.bin", b"a", root)
    b = drive.file("b.bin", b"b", sub)
    c = drive.file("c.bin", b"c", deep)
    drive.shortcut("link-to-c.bin", c, root)
    drive.folder("empty", root)

    files = {str(f.relative_path): f async for f in client.walk(root)}

    assert set(files) == {"a.bin", "sub/b.bin", "sub/deep/c.bin", "link-to-c.bin"}
    assert files["a.bin"].id == a
    assert files["sub/b.bin"].id == b
    assert files["link-to-c.bin"].id == c  # shortcut points at the real file
    assert files["link-to-c.bin"].size == 1


async def test_walk_follows_pagination(drive: FakeDrive, client: DriveClient) -> None:
    drive.page_size = 2
    root = drive.folder("root")
    for i in range(5):
        drive.file(f"f{i}", b"x", root)

    names = sorted([f.name async for f in client.walk(root)])

    assert names == [f"f{i}" for i in range(5)]
    assert sum(1 for r in drive.requests if r.url.path.endswith("/files")) == 3


async def test_walk_sanitizes_path_separators(
    drive: FakeDrive, client: DriveClient
) -> None:
    root = drive.folder("root")
    drive.file("../evil/name.txt", b"x", root)

    [f] = [f async for f in client.walk(root)]

    assert f.relative_path == PurePosixPath(".._evil_name.txt")


# ---------------------------------------------------------------- download


async def test_download_writes_complete_file(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    dest = tmp_path / "big.bin"
    seen: list[int] = []

    await client.download(await _file(client, fid), dest, on_progress=seen.append)

    assert dest.read_bytes() == DATA
    assert not dest.with_name("big.bin.part").exists()
    assert sum(seen) == len(DATA)
    assert "Range" not in drive.media_requests(fid)[0].headers


async def test_download_resumes_existing_part(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    dest = tmp_path / "big.bin"
    write_part(dest, DATA[:300])  # simulate an interrupted previous run
    seen: list[int] = []

    await client.download(await _file(client, fid), dest, on_progress=seen.append)

    assert dest.read_bytes() == DATA
    [req] = drive.media_requests(fid)
    assert req.headers["Range"] == "bytes=300-"
    assert sum(seen) == len(DATA)  # the 300 already on disk still count


async def test_download_complete_part_needs_no_request(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    dest = tmp_path / "big.bin"
    write_part(dest, DATA)

    await client.download(await _file(client, fid), dest)

    assert dest.read_bytes() == DATA
    assert drive.media_requests(fid) == []


async def test_download_restarts_when_server_ignores_range(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    drive.ignore_range = True
    fid = drive.file("big.bin", DATA, None)
    dest = tmp_path / "big.bin"
    write_part(dest, b"GARBAGE" * 10)
    seen: list[int] = []

    await client.download(await _file(client, fid), dest, on_progress=seen.append)

    assert dest.read_bytes() == DATA  # not "GARBAGE..." + DATA
    assert sum(seen) == len(DATA)  # progress was rewound for the discarded bytes


async def test_download_discards_oversized_part(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    dest = tmp_path / "big.bin"
    write_part(dest, DATA + b"extra")

    await client.download(await _file(client, fid), dest)

    assert dest.read_bytes() == DATA
    assert "Range" not in drive.media_requests(fid)[0].headers


async def test_download_recovers_from_416(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    dest = tmp_path / "big.bin"
    write_part(dest, DATA[:300])
    # Metadata without a size makes the client trust the .part and ask for a Range;
    # the server (whose file is shorter) answers 416.
    drive.nodes[fid].content = DATA[:200]
    meta = await _file(client, fid)
    meta = DriveFile(meta.id, meta.name, meta.mime_type, None, None, meta.relative_path)

    await client.download(meta, dest)

    assert dest.read_bytes() == DATA[:200]
    statuses = [r.headers.get("Range") for r in drive.media_requests(fid)]
    assert statuses == ["bytes=300-", None]


async def test_download_resumes_after_midstream_disconnect(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    # Both cut-offs are multiples of chunk_size (4): a trailing partial chunk
    # never leaves httpx's chunker, so it would simply be re-fetched and the
    # resume offsets below would not be exact.
    drive.fail_after[fid] = deque([100, 48])  # die twice, then succeed
    dest = tmp_path / "big.bin"

    await client.download(await _file(client, fid), dest)

    assert dest.read_bytes() == DATA
    ranges = [r.headers.get("Range") for r in drive.media_requests(fid)]
    assert ranges[0] is None
    assert ranges[1] == "bytes=100-"
    assert ranges[2] == "bytes=148-"


async def test_download_gives_up_after_max_retries_but_keeps_part(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    drive.fail_after[fid] = deque([12] * 10)  # never succeeds within max_retries=3
    dest = tmp_path / "big.bin"

    with pytest.raises(httpx.TransportError):
        await client.download(await _file(client, fid), dest)

    part = dest.with_name("big.bin.part")
    assert not dest.exists()
    # 1 attempt + 3 retries, each appending 12 bytes (a multiple of chunk_size=4)
    assert part.exists() and part.read_bytes() == DATA[:48]


async def test_download_no_resume_starts_from_scratch(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    dest = tmp_path / "big.bin"
    write_part(dest, DATA[:300])

    await client.download(await _file(client, fid), dest, resume=False)

    assert dest.read_bytes() == DATA
    assert "Range" not in drive.media_requests(fid)[0].headers


async def test_download_retries_on_429(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    drive.status_queue[fid] = deque([429, 503])
    dest = tmp_path / "big.bin"

    await client.download(await _file(client, fid), dest)

    assert dest.read_bytes() == DATA
    assert len(drive.media_requests(fid)) == 3


async def test_download_permission_403_is_not_retried(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.file("big.bin", DATA, None)
    drive.deny.add(fid)

    with pytest.raises(DriveError, match="403"):
        await client.download(await _file(client, fid), tmp_path / "big.bin")

    assert len(drive.media_requests(fid)) == 1


async def test_export_google_doc_never_resumes(
    drive: FakeDrive, client: DriveClient, tmp_path: Path
) -> None:
    fid = drive.gdoc("Notes", None, exported=b"<docx bytes>")
    meta = await _file(client, fid)
    dest = tmp_path / meta.local_name
    write_part(dest, b"stale")

    await client.download(meta, dest)

    assert dest.name == "Notes.docx"
    assert dest.read_bytes() == b"<docx bytes>"
    [req] = [r for r in drive.requests if r.url.path.endswith("/export")]
    assert "Range" not in req.headers


async def test_walk_reports_every_file_as_it_is_found(
    drive: FakeDrive, client: DriveClient
) -> None:
    """`on_file` must fire inside the concurrent descent, not just at the top.

    walk() yields a subfolder's files only after gathering the whole branch, so
    a caller counting yields sees nothing while the tree is being walked. The
    callback is what makes a live count possible.
    """
    root = drive.folder("root")
    sub = drive.folder("sub", root)
    deep = drive.folder("deep", sub)
    drive.file("a.bin", b"a", root)
    drive.file("b.bin", b"b", sub)
    drive.file("c.bin", b"c", deep)

    seen: list[str] = []
    yielded = [
        f.name async for f in client.walk(root, on_file=lambda f: seen.append(f.name))
    ]

    assert sorted(seen) == ["a.bin", "b.bin", "c.bin"]
    assert sorted(yielded) == sorted(seen)


async def test_walk_without_callback_is_unchanged(
    drive: FakeDrive, client: DriveClient
) -> None:
    root = drive.folder("root")
    drive.file("a.bin", b"a", root)

    assert [f.name async for f in client.walk(root)] == ["a.bin"]
