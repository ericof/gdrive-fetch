# gdrive-fetch

A `gdown`-style downloader for **private** Google Drive folders, using the
authenticated Drive v3 API with asyncio and a configurable number of parallel
transfers.

- Recursive folder mirroring (subfolders, shortcuts resolved, shared drives supported)
- `-j N` parallel downloads (bounded by an `asyncio.Semaphore`)
- Resumable downloads (like `gdown --continue`): interrupted transfers leave a `.part`
  file that the next run continues via HTTP `Range`; md5 verified; unchanged files skipped
- Google Docs/Sheets/Slides exported as `.docx`/`.xlsx`/`.pptx`
- Retries with exponential backoff on 429/5xx/quota 403
- Usable as a library or a CLI

## Setup

```sh
uv sync
```

### Credentials (pick one)

**Service account** — best for headless/automation. Create a key in Google Cloud
Console (Drive API enabled), then *share the folder with the service account's
email*; it cannot see your files otherwise.

```sh
uv run gdrive-fetch <FOLDER_URL_OR_ID> -o ./out -j 8 --service-account sa.json
```

**OAuth user** — downloads as *you*. Create an "OAuth client ID" of type
*Desktop app*, download `client_secrets.json`, and run once interactively; the
token is cached in `~/.config/gdrive-fetch/token.json`.

```sh
uv run gdrive-fetch <FOLDER_URL_OR_ID> -o ./out -j 8 --client-secrets client_secrets.json
```

Subsequent runs need neither flag.

### Resuming

Resume is on by default. Kill a run mid-way and start it again: complete files
are skipped (size + md5), `.part` files continue from their current byte offset,
and only then is md5 checked. Transient disconnects mid-transfer are also
resumed in-process with a `Range` request, without restarting the file.
Pass `--no-resume` to discard `.part` files and start clean.

Google-native exports (Docs/Sheets/Slides) are never resumed — the export
endpoint neither reports a size nor honours `Range`.

## Library use

```python
import asyncio
from pathlib import Path
from gdrive_fetch import DriveClient, TokenProvider, download, load_credentials


async def main() -> None:
    creds = load_credentials(service_account_file=Path("sa.json"))
    async with DriveClient(TokenProvider(creds)) as client:
        report = await download(client, "FOLDER_ID", Path("out"), concurrency=8)
    print(len(report.downloaded), "files;", len(report.failed), "failures")


asyncio.run(main())
```

`client.walk(folder_id)` is an async iterator of `DriveFile` if you only want the tree.

## Development

```sh
uv run ruff check . && uv run mypy src && uv run pytest
```

Tests run against an in-memory Drive v3 fake (`tests/conftest.py`) served
through `httpx.MockTransport`, so they exercise real `Range`/`206`/`416`
handling, pagination and mid-stream disconnects without touching the network.
