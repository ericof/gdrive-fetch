from gdrive_fetch.cli import _render_plan
from gdrive_fetch.cli import build_parser
from gdrive_fetch.cli import extract_id
from gdrive_fetch.downloader import Action
from gdrive_fetch.downloader import DownloadPlan
from gdrive_fetch.downloader import FilePlan
from gdrive_fetch.models import DriveFile
from pathlib import Path
from pathlib import PurePosixPath

import pytest


@pytest.mark.parametrize(
    "value",
    [
        "1AbCdEfGhIjKlMnOpQrStUvWxYz",
        "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz",
        "https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz?usp=sharing",
        "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view",
        "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWxYz",
    ],
)
def test_extract_id(value: str) -> None:
    assert extract_id(value) == "1AbCdEfGhIjKlMnOpQrStUvWxYz"


def test_dry_run_defaults_to_off() -> None:
    assert build_parser().parse_args(["1AbCdEfGhIjKlMnOpQrStUvWxYz"]).dry_run is False


@pytest.mark.parametrize("flag", ["-n", "--dry-run"])
def test_dry_run_flag(flag: str) -> None:
    args = build_parser().parse_args(["1AbCdEfGhIjKlMnOpQrStUvWxYz", flag])
    assert args.dry_run is True


def _flat(captured: str) -> str:
    """Collapse rich's wrapping so assertions can look for whole phrases."""
    return " ".join(captured.split())


def _entry(name: str, action: Action, size: int | None, out: Path) -> FilePlan:
    file = DriveFile("i" * 10, name, "text/plain", size, None, PurePosixPath(name))
    return FilePlan(file, out / name, action, "because", 0)


def test_render_plan_prints_every_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = DownloadPlan([
        _entry("a.bin", Action.DOWNLOAD, 10, tmp_path),
        _entry("b.bin", Action.SKIP, 20, tmp_path),
        _entry("c.bin", Action.RESUME, 30, tmp_path),
        _entry("Notes.docx", Action.DOWNLOAD, None, tmp_path),
    ])

    _render_plan(result, tmp_path)
    out = _flat(capsys.readouterr().out)

    for name in ("a.bin", "b.bin", "c.bin", "Notes.docx"):
        assert name in out
    assert "2 to download" in out
    assert "1 to resume" in out
    assert "1 up to date" in out
    assert "+1 of unknown size" in out  # the export has no size to report


def test_render_plan_handles_an_empty_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _render_plan(DownloadPlan(), tmp_path)
    out = _flat(capsys.readouterr().out)

    assert "nothing to do" in out
    assert "0 to download" in out
