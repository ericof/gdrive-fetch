from __future__ import annotations

from .auth import DEFAULT_TOKEN_FILE
from .auth import load_credentials
from .auth import TokenProvider
from .client import DriveClient
from .downloader import download
from pathlib import Path
from rich.console import Console

import argparse
import asyncio
import re
import sys


_ID_PATTERNS = (
    re.compile(r"/folders/([A-Za-z0-9_-]{10,})"),
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)


def extract_id(value: str) -> str:
    """Accept a bare id or any of the usual Drive URL shapes (like gdown)."""
    for pat in _ID_PATTERNS:
        if m := pat.search(value):
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    raise argparse.ArgumentTypeError(f"cannot find a Drive id in {value!r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gdrive-fetch",
        description=(
            "Download a private Google Drive folder (or file) via the Drive API."
        ),
    )
    p.add_argument("target", type=extract_id, help="Folder/file id or Drive URL")
    p.add_argument(
        "-o", "--output", type=Path, default=Path.cwd(), help="Destination directory"
    )
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=4,
        help="Number of parallel downloads (default: 4)",
    )
    auth = p.add_argument_group("authentication")
    auth.add_argument(
        "--service-account", type=Path, help="Service account JSON key file"
    )
    auth.add_argument(
        "--client-secrets", type=Path, help="OAuth 'installed app' client secrets JSON"
    )
    auth.add_argument(
        "--token",
        type=Path,
        default=DEFAULT_TOKEN_FILE,
        help=f"Where to cache the OAuth user token (default: {DEFAULT_TOKEN_FILE})",
    )
    p.add_argument("--no-verify", action="store_true", help="Skip md5 verification")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download files even if they already match",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="Discard partial (.part) files instead of continuing them",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="No progress bars")
    return p


async def _run(args: argparse.Namespace) -> int:
    console = Console(stderr=True, quiet=args.quiet)
    creds = load_credentials(
        service_account_file=args.service_account,
        client_secrets_file=args.client_secrets,
        token_file=args.token,
    )
    async with DriveClient(TokenProvider(creds)) as client:
        report = await download(
            client,
            args.target,
            args.output,
            concurrency=args.jobs,
            verify=not args.no_verify,
            skip_existing=not args.overwrite,
            resume=not args.no_resume,
            quiet=args.quiet,
        )

    console.print(
        f"[green]{len(report.downloaded)} downloaded[/], "
        f"[yellow]{len(report.skipped)} skipped[/], "
        f"[red]{len(report.failed)} failed[/]"
    )
    for path, exc in report.failed.items():
        console.print(f"  [red]✗[/] {path}: {exc}")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
