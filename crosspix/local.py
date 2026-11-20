"""Local folder photo provider using Pillow (PIL) and piexif for image/EXIF metadata."""

from __future__ import annotations

from datetime import datetime
import hashlib
import mimetypes
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Optional

import piexif
from PIL import ExifTags, Image

from crosspix.base import PhotoItem, PhotoProvider

SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".gif",
    ".tiff",
    ".tif",
    ".bmp",
}


class LocalProvider(PhotoProvider):
    """Photo provider for synchronizing photos stored in local directories."""

    platform_name = "local"

    def __init__(self, root_dir: str | Path, recursive: bool = True) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.recursive = recursive

    def connect(self) -> None:
        """Ensure the local root directory exists."""
        self.root_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def compute_sha256(file_path: Path) -> str:
        """Compute the SHA-256 hex digest of a local file."""
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def extract_image_metadata(
        file_path: Path,
    ) -> tuple[Optional[int], Optional[int], Optional[datetime], dict[str, Any]]:
        """Read image dimensions and EXIF capture timestamp via Pillow and piexif."""
        width: Optional[int] = None
        height: Optional[int] = None
        created_at: Optional[datetime] = None
        exif_summary: dict[str, Any] = {}

        try:
            with Image.open(file_path) as img:
                width, height = img.size
                raw_exif = img.getexif()
                if raw_exif:
                    for tag_id, value in raw_exif.items():
                        tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                        if isinstance(value, (str, int, float)):
                            exif_summary[str(tag_name)] = value

            # Check piexif DateTimeOriginal / DateTimeDigitized
            if file_path.suffix.lower() in {".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
                try:
                    exif_dict = piexif.load(str(file_path))
                    exif_ifd = exif_dict.get("Exif", {})
                    zeroth_ifd = exif_dict.get("0th", {})
                    dt_bytes = (
                        exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
                        or exif_ifd.get(piexif.ExifIFD.DateTimeDigitized)
                        or zeroth_ifd.get(piexif.ImageIFD.DateTime)
                    )
                    if isinstance(dt_bytes, bytes):
                        dt_str = dt_bytes.decode("utf-8", errors="ignore").strip("\x00 ")
                        created_at = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                except Exception:
                    pass
        except Exception:
            pass

        if created_at is None:
            stat = file_path.stat()
            created_at = datetime.fromtimestamp(stat.st_mtime)

        return width, height, created_at, exif_summary

    def _file_to_photo_item(self, file_path: Path) -> PhotoItem:
        stat = file_path.stat()
        width, height, created_at, exif_meta = self.extract_image_metadata(file_path)
        mime_type, _ = mimetypes.guess_type(file_path.name)
        rel_path = file_path.relative_to(self.root_dir)
        album = rel_path.parent.name if rel_path.parent != Path(".") else None

        return PhotoItem(
            id=str(rel_path),
            filename=file_path.name,
            platform=self.platform_name,
            checksum=self.compute_sha256(file_path),
            created_at=created_at,
            size=stat.st_size,
            width=width,
            height=height,
            mime_type=mime_type or "image/jpeg",
            album=album,
            local_path=file_path,
            raw_metadata=exif_meta,
        )

    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """Yield PhotoItem entries from the local directory."""
        search_root = self.root_dir / album if album else self.root_dir
        if not search_root.exists():
            return

        iterator = (
            search_root.rglob("*") if self.recursive and not album else search_root.glob("*")
        )
        for path in sorted(iterator):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield self._file_to_photo_item(path)

    def download_photo(self, item: PhotoItem, destination_dir: Path) -> Path:
        """Copy a photo from the local provider directory into destination_dir."""
        destination_dir = Path(destination_dir).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)
        source_path = item.local_path or (self.root_dir / item.id)
        if not source_path.exists():
            raise FileNotFoundError(f"Local photo not found: {source_path}")

        target_path = destination_dir / item.filename
        if source_path.resolve() != target_path.resolve():
            shutil.copy2(source_path, target_path)
        return target_path

    def upload_photo(
        self,
        file_path: Path,
        item: Optional[PhotoItem] = None,
        album: Optional[str] = None,
    ) -> PhotoItem:
        """Copy a photo file into the local provider directory (and optional album subfolder)."""
        file_path = Path(file_path).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"Source file does not exist: {file_path}")

        target_album = album or (item.album if item else None)
        target_dir = self.root_dir / target_album if target_album else self.root_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        target_filename = item.filename if item and item.filename else file_path.name
        target_path = target_dir / target_filename

        if file_path != target_path:
            shutil.copy2(file_path, target_path)

        if item and item.created_at:
            ts = item.created_at.timestamp()
            os.utime(target_path, (ts, ts))

        return self._file_to_photo_item(target_path)
