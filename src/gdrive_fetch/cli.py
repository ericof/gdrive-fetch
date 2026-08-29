from __future__ import annotations

from .auth import DEFAULT_TOKEN_FILE
from .auth import load_credentials
from .auth import TokenProvider
from .client import DriveClient
from .downloader import Action
from .downloader import download
from .downloader import DownloadPlan
from .downloader import plan
from pathlib import Path
from rich.console import Console
from rich.filesize import decimal
from rich.table import Table

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
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="List what would be transferred and exit; downloads nothing",
    )
    return p


_ACTION_STYLE = {
    Action.DOWNLOAD: "green",
    Action.RESUME: "cyan",
    Action.SKIP: "yellow",
}


def _render_plan(result: DownloadPlan, dest_dir: Path) -> None:
    """Print a dry run's verdict, one row per file plus a summary.

    Goes to stdout rather than stderr: for a dry run the listing is the
    output, not progress chatter.

    :param result: the plan to render.
    :param dest_dir: the output directory the plan was computed against.
    """
    console = Console()
    table = Table(header_style="bold", expand=False)
    table.add_column("action")
    table.add_column("file")
    table.add_column("size", justify="right")
    table.add_column("why", style="dim")
    for entry in result.entries:
        size = "?" if entry.file.size is None else decimal(entry.file.size)
        style = _ACTION_STYLE[entry.action]
        table.add_row(
            f"[{style}]{entry.action.value}[/]",
            str(entry.dest.relative_to(dest_dir)),
            size,
            entry.reason,
        )
    if result.entries:
        console.print(table)
    else:
        console.print("[dim]nothing to do: no files found[/]")

    unknown = ""
    if result.unknown_sizes:
        unknown = f" (+{result.unknown_sizes} of unknown size)"
    console.print(
        f"[green]{len(result.of(Action.DOWNLOAD))} to download[/], "
        f"[cyan]{len(result.of(Action.RESUME))} to resume[/], "
        f"[yellow]{len(result.of(Action.SKIP))} up to date[/] — "
        f"{decimal(result.transfer_bytes)} to transfer{unknown} into {dest_dir}"
    )


async def _run(args: argparse.Namespace) -> int:
    console = Console(stderr=True, quiet=args.quiet)
    creds = load_credentials(
        service_account_file=args.service_account,
        client_secrets_file=args.client_secrets,
        token_file=args.token,
    )
    async with DriveClient(TokenProvider(creds)) as client:
        if args.dry_run:
            result = await plan(
                client,
                args.target,
                args.output,
                verify=not args.no_verify,
                skip_existing=not args.overwrite,
                resume=not args.no_resume,
            )
            _render_plan(result, args.output)
            return 0

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
