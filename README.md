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
- `--dry-run` compares Drive against your output folder without transferring
- Usable as a library or a CLI

## Setup

```sh
make install
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

### Dry run

`-n` / `--dry-run` fetches metadata only, compares it against what is already
in `-o`, and prints the verdict per file. Nothing is downloaded, written or
deleted — an unusable `.part` file is reported but left alone, where a real run
would remove it.

```sh
uv run gdrive-fetch <FOLDER_URL_OR_ID> -o ./out --dry-run
```

```
 action     file                 size   why
 download   changed.bin        1.0 kB   md5 differs
 skip       done.bin           1.0 kB   size and md5 match
 download   fresh.bin          5.0 kB   missing locally
 download   Meeting Notes.docx      ?   missing locally
 resume     partial.bin        1.0 kB   300 bytes already in .part
 download   reports/q3.bin   900.0 kB   missing locally

4 to download, 1 to resume, 1 up to date — 906.7 kB to transfer
(+1 of unknown size) into ./out
```

Rows are ordered by destination path, ignoring case, so the report reads the
same way twice running — Drive itself returns children in no useful order.

The comparison honours the same flags as a real run, so `--no-verify` (size
only, no md5), `--overwrite` and `--no-resume` all change the plan the way they
would change the download. Google-native exports have no size or md5 to compare,
so they are always listed as `download` with an unknown size.

Both phases report progress while they work: a spinner and a running count
while the Drive tree is listed (its size is unknown until it has been walked),
then a bar while each file is compared against the output folder. On a large
folder the comparison is the slow half, because verification hashes every file
that is already there — pass `--no-verify` to compare on size alone, which
needs only a `stat()` per file. `-q` silences both; the table still prints.

Note that `-j` only bounds parallel *transfers*: a dry run never transfers, and
compares files one at a time.

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
`plan(client, file_id, dest_dir)` returns the same comparison the CLI renders,
as a `DownloadPlan` of `FilePlan` entries.

## Development

```sh
make check && make test
```

Tests run against an in-memory Drive v3 fake (`tests/conftest.py`) served
through `httpx.MockTransport`, so they exercise real `Range`/`206`/`416`
handling, pagination and mid-stream disconnects without touching the network.
