from __future__ import annotations

from .auth import TokenProvider
from .models import DriveFile
from .models import FOLDER_MIME
from .models import SHORTCUT_MIME
from collections.abc import AsyncIterator
from collections.abc import Callable
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

import asyncio
import httpx
import random


API_BASE = "https://www.googleapis.com/drive/v3"
FILE_FIELDS = "id,name,mimeType,size,md5Checksum,shortcutDetails"
LIST_FIELDS = f"nextPageToken,files({FILE_FIELDS})"
RETRYABLE = {403, 408, 429, 500, 502, 503, 504}

ProgressCallback = Callable[[int], None]


class DriveError(RuntimeError):
    pass


class DriveClient:
    """Thin async wrapper over the Drive v3 REST API (read-only operations)."""

    def __init__(
        self,
        tokens: TokenProvider,
        *,
        max_retries: int = 5,
        timeout: float = 60.0,
        chunk_size: int = 1 << 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._tokens = tokens
        self._max_retries = max_retries
        self._chunk_size = chunk_size
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> DriveClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ http

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {await self._tokens.token()}"}
        if extra:
            headers.update(extra)
        return headers

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            resp = await self._http.get(
                path, params=params, headers=await self._headers()
            )
            if resp.status_code == 200:
                return dict(resp.json())
            if not _should_retry(resp) or attempt == self._max_retries:
                raise DriveError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
            await asyncio.sleep(_backoff(attempt))
        raise AssertionError("unreachable")

    # --------------------------------------------------------------- metadata

    async def get_file(self, file_id: str) -> DriveFile:
        data = await self._get_json(
            f"/files/{file_id}", {"fields": FILE_FIELDS, "supportsAllDrives": "true"}
        )
        return _to_drive_file(data, PurePosixPath(data["name"]))

    async def list_children(self, folder_id: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": LIST_FIELDS,
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        items: list[dict[str, Any]] = []
        while True:
            page = await self._get_json("/files", params)
            items.extend(page.get("files", []))
            token = page.get("nextPageToken")
            if not token:
                return items
            params["pageToken"] = token

    async def walk(
        self, folder_id: str, prefix: PurePosixPath | None = None
    ) -> AsyncIterator[DriveFile]:
        """Yield every non-folder file under `folder_id`, depth-first.

        Shortcuts are resolved to their targets. Subfolders are listed concurrently.

        :param folder_id: id of the folder to descend into.
        :param prefix: path the yielded files are made relative to.
        :returns: an async iterator over every non-folder descendant.
        """
        prefix = PurePosixPath() if prefix is None else prefix
        children = await self.list_children(folder_id)
        subfolders: list[tuple[str, PurePosixPath]] = []
        for item in children:
            if item["mimeType"] == SHORTCUT_MIME:
                target_id = item.get("shortcutDetails", {}).get("targetId")
                if not target_id:
                    continue
                target = await self.get_file(target_id)
                item = {
                    "id": target.id,
                    "name": item["name"],  # keep the shortcut's name
                    "mimeType": target.mime_type,
                    "size": target.size,
                    "md5Checksum": target.md5,
                }
            if item["mimeType"] == FOLDER_MIME:
                subfolders.append((item["id"], prefix / _safe_name(item["name"])))
            else:
                yield _to_drive_file(item, prefix / _safe_name(item["name"]))

        # Listing is cheap; fan out over subfolders but drain them sequentially
        # so ordering stays deterministic per branch.
        results = await asyncio.gather(*[
            _collect(self.walk(fid, sub)) for fid, sub in subfolders
        ])
        for branch in results:
            for f in branch:
                yield f

    # ---------------------------------------------------------------- download

    async def _write_body(
        self,
        resp: httpx.Response,
        part: Path,
        start: int,
        on_progress: ProgressCallback | None,
    ) -> int:
        """Append a streaming response body to the partial file.

        A ``200`` answer to a ranged request means the server ignored the
        ``Range``: what is already on disk is not a prefix of what is being
        sent, so it is discarded and the progress for it rewound.

        :param resp: an open 200/206 response whose body has not been read.
        :param part: the ``.part`` file being filled.
        :param start: how many bytes ``part`` currently holds.
        :param on_progress: called with every delta, rewind included.
        :returns: how many bytes ``part`` holds once the body is consumed.
        """
        if resp.status_code == 200 and start:
            if on_progress:
                on_progress(-start)
            start = 0
        with part.open("ab" if start else "wb") as fh:
            async for chunk in resp.aiter_bytes(self._chunk_size):
                fh.write(chunk)
                start += len(chunk)
                if on_progress:
                    on_progress(len(chunk))
        return start

    async def download(  # noqa: C901 - a retry state machine, kept in one piece
        self,
        file: DriveFile,
        dest: Path,
        *,
        resume: bool = True,
        on_progress: ProgressCallback | None = None,
    ) -> Path:
        """Stream a file to `dest`.

        Bytes go to `dest.part` first and the file is renamed only when complete,
        so an interrupted run never leaves a truncated `dest` behind. On the next
        run (or after a transient network error mid-transfer) the partial file is
        resumed with an HTTP ``Range`` request, like ``gdown --continue``.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        path, params, resumable = _endpoint(file)

        if not (resume and resumable):
            part.unlink(missing_ok=True)

        start = _partial_offset(part, file.size)
        if on_progress and start:
            # Bytes already on disk count towards the file's total: callers size
            # their progress against file.size, not against the remainder.
            on_progress(start)
        if start and start == file.size:
            # Everything already on disk; just finalize (caller verifies md5).
            return _finalize(part, dest, file)

        attempt = 0
        while True:
            extra = _range_header(start)
            try:
                async with self._http.stream(
                    "GET", path, params=params, headers=await self._headers(extra)
                ) as resp:
                    if resp.status_code == 416:
                        # Server disagrees about where we are; the partial
                        # file is untrustworthy.
                        await resp.aread()
                        start = _rewind(part, start, on_progress)
                        attempt += 1
                        if attempt > self._max_retries:
                            raise DriveError(
                                f"download {file.id}: persistent 416 on resume"
                            )
                        continue
                    if resp.status_code not in (200, 206):
                        body = (await resp.aread())[:300]
                        if _should_retry(resp) and attempt < self._max_retries:
                            attempt += 1
                            await asyncio.sleep(_backoff(attempt))
                            continue
                        raise DriveError(
                            f"download {file.id} -> {resp.status_code}: {body!r}"
                        )

                    start = await self._write_body(resp, part, start, on_progress)
                break
            except httpx.TransportError:
                # Connection dropped mid-stream: keep what we have and
                # resume from there.
                if attempt >= self._max_retries:
                    raise
                attempt += 1
                start = part.stat().st_size if part.exists() else 0
                await asyncio.sleep(_backoff(attempt))

        return _finalize(part, dest, file)


# ------------------------------------------------------------------ helpers


def _range_header(start: int) -> dict[str, str] | None:
    """Build the ``Range`` header for resuming at `start`.

    :param start: byte offset already held on disk.
    :returns: the header mapping, or ``None`` to request the whole file.
    """
    return {"Range": f"bytes={start}-"} if start else None


def _rewind(part: Path, start: int, on_progress: ProgressCallback | None) -> int:
    """Throw away a partial file and the progress already credited for it.

    :param part: the ``.part`` file to remove; it need not exist.
    :param start: how many of its bytes were reported to `on_progress`.
    :param on_progress: called with the negative delta, keeping the caller's
        running total equal to what is actually on disk.
    :returns: ``0``, the offset to restart from.
    """
    part.unlink(missing_ok=True)
    if on_progress and start:
        on_progress(-start)
    return 0


def _finalize(part: Path, dest: Path, file: DriveFile) -> Path:
    """Promote a completed ``.part`` file to its final name.

    :param part: the partial file, expected to be complete.
    :param dest: the path it should take.
    :param file: the Drive metadata the result is checked against.
    :raises DriveError: if fewer bytes arrived than Drive advertised. The
        ``.part`` file is left behind so the next run can resume it.
    :returns: `dest`.
    """
    written = part.stat().st_size
    if file.size is not None and written != file.size:
        raise DriveError(
            f"download {file.id}: short read ({written}/{file.size} bytes)"
        )
    part.replace(dest)
    return dest


def _endpoint(file: DriveFile) -> tuple[str, dict[str, str], bool]:
    """Resolve how a file is fetched.

    :param file: the file about to be downloaded.
    :returns: the API path, its query parameters, and whether ``Range``
        requests may be used to resume it.
    """
    if file.export:
        # Exports have no stable size and ignore Range: never resume them.
        return f"/files/{file.id}/export", {"mimeType": file.export[0]}, False
    return f"/files/{file.id}", {"alt": "media", "supportsAllDrives": "true"}, True


def usable_partial(part: Path, size: int | None) -> int:
    """Return how many bytes of an existing ``.part`` file are worth keeping.

    A partial longer than the file itself cannot be a prefix of it, so none of
    it is usable. This only ever calls ``stat()``: it is the read-only half of
    :func:`_partial_offset`, and `plan` relies on it leaving the disk alone.

    :param part: the ``.part`` file, which need not exist.
    :param size: the file's size as Drive reports it, or ``None`` when unknown.
    :returns: the byte offset to resume from; ``0`` to start over.
    """
    start = part.stat().st_size if part.exists() else 0
    return 0 if size is not None and start > size else start


def _partial_offset(part: Path, size: int | None) -> int:
    """As :func:`usable_partial`, but delete a partial that cannot be resumed.

    :param part: the ``.part`` file, which need not exist.
    :param size: the file's size as Drive reports it, or ``None`` when unknown.
    :returns: the byte offset to resume from; ``0`` to start over.
    """
    start = usable_partial(part, size)
    if start == 0 and part.exists() and part.stat().st_size:
        part.unlink()
    return start


def _to_drive_file(item: dict[str, Any], rel: PurePosixPath) -> DriveFile:
    size = item.get("size")
    return DriveFile(
        id=item["id"],
        name=item["name"],
        mime_type=item["mimeType"],
        size=int(size) if size is not None else None,
        md5=item.get("md5Checksum"),
        relative_path=rel,
    )


def _safe_name(name: str) -> str:
    # Drive names may contain path separators; keep them from escaping the tree.
    return name.replace("/", "_").replace("\\", "_").strip() or "_"


async def _collect(it: AsyncIterator[DriveFile]) -> list[DriveFile]:
    return [f async for f in it]


def _should_retry(resp: httpx.Response) -> bool:
    if resp.status_code not in RETRYABLE:
        return False
    if resp.status_code == 403:
        # 403 is also "no permission"; only retry the quota flavour.
        text = resp.text.lower()
        return "rate" in text or "quota" in text or "userratelimit" in text
    return True


def _backoff(attempt: int) -> float:
    # Jitter only spreads retries apart; it is not a security primitive.
    return min(30.0, (2**attempt) + random.uniform(0, 1))  # noqa: S311
