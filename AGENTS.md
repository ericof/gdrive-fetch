# Agents — gdrive-fetch

Authenticated, async, parallel downloader for private Google Drive folders.
Think `gdown`, but via the Drive v3 API with `-j N` concurrency and `gdown --continue`
resume semantics. Small, single-purpose, read-only.

## Commands

```sh
make sync                                  # install (creates .venv)
make lint
make format
make lint-mypy                         # strict; tests are not type-checked
uv run pytest                            # no network needed
uv run gdrive-fetch <ID_OR_URL> -o out -j 8 [--service-account sa.json | --client-secrets cs.json]
```
Run all three (make lint, lint-mypy, make test) before considering any change done.

## Layout

```
src/gdrive_fetch/
  models.py      DriveFile dataclass, mime constants, EXPORT_FORMATS
  auth.py        load_credentials() (SA / cached OAuth / interactive), TokenProvider
  client.py      DriveClient: httpx async REST client — list, walk, download, retries
  downloader.py  download(): tree collection, dedupe, semaphore, md5 verify, progress
  cli.py         argparse entry point (gdrive-fetch)
tests/
  conftest.py    FakeDrive: in-memory Drive v3 behind httpx.MockTransport
  test_client.py / test_downloader.py / test_cli.py
```

Dependency flow is strictly downward: cli → downloader → client → auth/models.
Don't import upward and don't let `models.py` import anything from the package.

## Design decisions (don't undo without a reason in the PR)

- **REST via `httpx`, not `google-api-python-client`.** The official client is
  sync-only. `google-auth` is used for credentials only; the blocking refresh
  runs in a thread behind a lock in `TokenProvider`.
- **Read-only scope** (`drive.readonly`). This tool never writes to Drive.
- **Parallelism is one `asyncio.Semaphore(concurrency)`** in `downloader.download`.
  Listing fans out with `gather` (cheap); transfers are bounded.
- **`.part` + rename.** `dest` only exists once complete. Interrupted runs leave
  `dest.part`, which is resumed with an HTTP `Range` request on the next run.
- **Verification is size + md5** (Drive reports md5 for binary files). A mismatch
  deletes `dest` and is reported as a failure; it does not abort other files.
- **Exports are never resumed.** Google-native files (Docs/Sheets/Slides) are
  exported to docx/xlsx/pptx; the export endpoint has no size and ignores `Range`.
- **403 is retried only when the body mentions rate/quota.** Permission 403s fail fast.
- **Duplicate names** in one folder get a `__<id[:8]>` suffix rather than clobbering.
- **No pre-commit.** Lint/type/test run via `uv run` and CI.

## Resume invariants (the contract `test_client.py` enforces)

When touching `DriveClient.download`, these must keep holding:

1. `.part == size` → finalize with **zero** requests.
2. `.part > size` → discard, start from 0.
3. `Range` answered with `200` → discard partial data, rewind progress, overwrite.
4. `416` → discard `.part`, retry from 0 (counts as an attempt).
5. `TransportError` mid-stream → resume from current `.part` size, appending.
6. After the loop, `.part != size` → raise **and keep the `.part`** for next run.
7. `resume=False` or an export → `.part` is unlinked before starting.

Progress callbacks receive byte deltas; negative deltas are used to rewind (case 3).

## Testing conventions

- Never hit the network in tests. Extend `FakeDrive` in `conftest.py` instead —
  it has knobs for pagination, `Range` handling, mid-stream disconnects
  (`fail_after`), status codes (`status_queue`), permission 403s (`deny`) and
  wire-vs-metadata mismatches (`serve_override`).
- The `client` fixture uses `chunk_size=4` and `max_retries=3`. Byte counts in
  failure-injection tests should be multiples of 4 so partial chunks aren't lost
  in httpx's buffer.
- `_backoff` is monkeypatched to 0 via an autouse fixture. Don't add sleeps.
- Every bug fix in resume logic gets a test named after the scenario
  (`test_download_<scenario>`), asserting on the `Range` headers `FakeDrive` recorded.

## Style

- Python 3.11+, `from __future__ import annotations`, `ruff` line length 100.
- `mypy --strict` on `src`. Prefer precise types over `Any`; `dict[str, Any]` is
  acceptable only for raw API JSON.
- Public API surface is `gdrive_fetch.__init__`; keep it small.
- No comments that restate code. Comment the *why* (e.g. why a 403 is or isn't retried).

## Autonomy protocol

Decide alone and just do it:
- Bug fixes with a regression test.
- New `FakeDrive` knobs needed by a test.
- Adding an export mapping to `EXPORT_FORMATS`.
- Refactors that keep the dependency flow and all tests green.
- CLI flags that are pure pass-throughs to existing `download()` kwargs.

Leave a note and stop (open a `NOTES.md` entry or a TODO in the PR body):
- Anything that would require a scope beyond `drive.readonly`.
- Adding a runtime dependency.
- Changing the on-disk layout (`.part` naming, dedupe suffix, token file location):
  users may have interrupted downloads relying on the current scheme.
- Changing retry/backoff policy or the 403 heuristic.
- Introducing state files or a database — this tool is intentionally stateless
  beyond `.part` files and the cached token.

If a test fails and the fix isn't obvious within one attempt, describe what you
observed and stop rather than loosening the assertion.

## Credentials (for humans; never commit these)

- `client_secrets.json`, `sa.json`, `~/.config/gdrive-fetch/token.json` are secrets.
  They are gitignored; keep it that way.
- OAuth apps left in *Testing* on the consent screen get refresh tokens that expire
  after 7 days. Publish the app or expect to re-auth.
- A service account only sees folders explicitly shared with its email.
