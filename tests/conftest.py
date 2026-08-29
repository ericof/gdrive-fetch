"""A small in-memory Google Drive v3 implementation served through httpx.MockTransport.

It models exactly the subset the client relies on: metadata, paginated listing,
``alt=media`` downloads honouring ``Range`` (206 / 200 / 416), Google-native
exports, and a few failure-injection knobs (mid-stream disconnects, status
codes, ignoring Range).
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from dataclasses import field
from gdrive_fetch.client import DriveClient
from gdrive_fetch.models import FOLDER_MIME
from gdrive_fetch.models import SHORTCUT_MIME
from itertools import count
from pathlib import Path

import asyncio
import gdrive_fetch.client as client_mod
import hashlib
import httpx
import pytest
import re


OCTET = "application/octet-stream"
GDOC = "application/vnd.google-apps.document"


@dataclass
class Node:
    id: str
    name: str
    mime: str
    parent: str | None
    content: bytes | None = None
    target: str | None = None  # for shortcuts

    def meta(self) -> dict[str, object]:
        d: dict[str, object] = {"id": self.id, "name": self.name, "mimeType": self.mime}
        if self.content is not None and self.mime != GDOC:
            d["size"] = str(len(self.content))
            d["md5Checksum"] = hashlib.md5(self.content).hexdigest()  # noqa: S324
        if self.target:
            d["shortcutDetails"] = {"targetId": self.target}
        return d


class FailingStream(httpx.AsyncByteStream):
    """Yields `good` bytes, then drops the connection."""

    def __init__(self, good: bytes) -> None:
        self._good = good

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._good:
            yield self._good
        raise httpx.ReadError("connection reset by fake server")


@dataclass
class FakeDrive:
    nodes: dict[str, Node] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    page_size: int = 1000
    ignore_range: bool = False
    #: file id -> number of bytes to send before failing (consumed once per entry)
    fail_after: dict[str, deque[int]] = field(default_factory=dict)
    #: file id -> status codes to return before serving normally
    status_queue: dict[str, deque[int]] = field(default_factory=dict)
    #: file ids that answer 403 (permission, not quota) on media requests
    deny: set[str] = field(default_factory=set)
    #: file id -> bytes actually served, when they must differ from the metadata
    serve_override: dict[str, bytes] = field(default_factory=dict)
    #: delay per media download, used by the concurrency test
    media_delay: float = 0.0
    inflight: int = 0
    max_inflight: int = 0
    _ids: count[int] = field(default_factory=lambda: count(1))

    # ---------------------------------------------------------------- builders

    def _new(self, name: str, mime: str, parent: str | None, **kw: object) -> str:
        nid = f"id{next(self._ids):04d}xxxxxxxx"
        self.nodes[nid] = Node(nid, name, mime, parent, **kw)  # type: ignore[arg-type]
        return nid

    def folder(self, name: str, parent: str | None = None) -> str:
        return self._new(name, FOLDER_MIME, parent)

    def file(
        self, name: str, content: bytes, parent: str | None, mime: str = OCTET
    ) -> str:
        return self._new(name, mime, parent, content=content)

    def gdoc(self, name: str, parent: str | None, exported: bytes = b"<docx>") -> str:
        return self._new(name, GDOC, parent, content=exported)

    def shortcut(self, name: str, target: str, parent: str | None) -> str:
        return self._new(name, SHORTCUT_MIME, parent, target=target)

    def media_requests(self, file_id: str) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.url.path.endswith(f"/files/{file_id}")
            and r.url.params.get("alt") == "media"
        ]

    # ----------------------------------------------------------------- server

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/drive/v3")

        if path == "/files":
            return self._list(request)
        m = re.fullmatch(r"/files/([^/]+)(/export)?", path)
        assert m, path
        node = self.nodes.get(m.group(1))
        if node is None:
            return httpx.Response(404, json={"error": {"message": "not found"}})
        if m.group(2):
            return httpx.Response(200, content=node.content or b"")
        if request.url.params.get("alt") == "media":
            return await self._media(node, request)
        return httpx.Response(200, json=node.meta())

    def _list(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")
        pm = re.search(r"'([^']+)' in parents", q)
        assert pm, q
        children = [n for n in self.nodes.values() if n.parent == pm.group(1)]
        offset = int(request.url.params.get("pageToken", "0"))
        page = children[offset : offset + self.page_size]
        body: dict[str, object] = {"files": [n.meta() for n in page]}
        if offset + self.page_size < len(children):
            body["nextPageToken"] = str(offset + self.page_size)
        return httpx.Response(200, json=body)

    async def _media(self, node: Node, request: httpx.Request) -> httpx.Response:
        assert node.content is not None
        if node.id in self.deny:
            return httpx.Response(
                403, json={"error": {"message": "insufficientFilePermissions"}}
            )
        queue = self.status_queue.get(node.id)
        if queue:
            return httpx.Response(
                queue.popleft(), json={"error": {"message": "rateLimitExceeded"}}
            )

        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.media_delay:
                await asyncio.sleep(self.media_delay)
        finally:
            self.inflight -= 1

        data = self.serve_override.get(node.id, node.content)
        total = len(data)
        status, headers, start = 200, {}, 0
        rng = request.headers.get("Range")
        if rng and not self.ignore_range:
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            if start >= total:
                return httpx.Response(
                    416, headers={"Content-Range": f"bytes */{total}"}
                )
            status = 206
            headers = {"Content-Range": f"bytes {start}-{total - 1}/{total}"}
            data = data[start:]

        fails = self.fail_after.get(node.id)
        if fails:
            good = fails.popleft()
            return httpx.Response(
                status, headers=headers, stream=FailingStream(data[:good])
            )
        return httpx.Response(status, headers=headers, content=data)


class FakeTokens:
    async def token(self) -> str:
        return "fake-token"


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()


@pytest.fixture
async def client(drive: FakeDrive) -> AsyncIterator[DriveClient]:
    async with DriveClient(
        FakeTokens(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(drive.handler),
        chunk_size=4,  # small chunks so partial writes happen before injected failures
        max_retries=3,
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod, "_backoff", lambda attempt: 0.0)


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324


def write_part(dest: Path, data: bytes) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(data)
    return part
