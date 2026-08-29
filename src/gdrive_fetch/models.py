from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# Google-native formats have no bytes to download; they must be exported.
EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
    "application/vnd.google-apps.script": (
        "application/vnd.google-apps.script+json",
        ".json",
    ),
}


@dataclass(slots=True, frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int | None
    md5: str | None
    relative_path: PurePosixPath  # path inside the root folder, including file name

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def export(self) -> tuple[str, str] | None:
        """(export mime type, extension) for Google-native files, else None."""
        return EXPORT_FORMATS.get(self.mime_type)

    @property
    def local_name(self) -> str:
        exp = self.export
        if exp and not self.name.endswith(exp[1]):
            return self.name + exp[1]
        return self.name
