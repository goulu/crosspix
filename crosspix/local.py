"""Local folder photo provider using Pillow (PIL) and piexif for image/EXIF metadata.

Implements the canonical `_library/YYYY/MM/` storage layout with hierarchical
`albums/<part1>/<part2>/...` folders linked via hardlinks or symlinks, using a space
delimiter (`" "`) by default to map flat remote album names to local folder trees.
"""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Iterable, Literal, Optional

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
    ".mp4",
    ".mov",
    ".m4v",
    ".3gp",
    ".avi",
    ".mkv",
    ".zip",
}

LinkMode = Literal["hardlink", "symlink", "copy"]


class LocalProvider(PhotoProvider):
    """Photo provider for synchronizing photos stored in local directories.

    Layout (Option 2):
    - `_library/YYYY/MM/<filename>`: Single physical copy of every photo.
    - `albums/<level1>/<level2>/<filename>`: Hardlinks or relative symlinks pointing
      to the canonical file in `_library/`, where `<level1>/<level2>` corresponds to
      a remote album name delimited by `album_delimiter` (default `" "`):
      e.g. Google Photos album `"Vacances 2026 Italie"` <-> `albums/Vacances/2026/Italie/`.
    """

    platform_name = "local"

    def __init__(
        self,
        root_dir: str | Path,
        recursive: bool = True,
        library_dirname: str = "_library",
        albums_dirname: str = "albums",
        album_delimiter: str = " ",
        link_mode: LinkMode = "hardlink",
        use_library_layout: bool = True,
        albums_only: bool = False,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.recursive = recursive
        self.library_dirname = library_dirname
        self.albums_dirname = albums_dirname
        self.album_delimiter = album_delimiter
        self.link_mode: LinkMode = link_mode
        self.use_library_layout = use_library_layout
        self.albums_only = albums_only
        self.on_progress = on_progress

    def _log(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)
        else:
            print(message, flush=True)

    @property
    def library_root(self) -> Path:
        return self.root_dir / self.library_dirname

    @property
    def albums_root(self) -> Path:
        return self.root_dir / self.albums_dirname

    def connect(self) -> None:
        """Ensure the local root directory exists."""
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def album_to_relpath(self, album_name: str) -> Path:
        """Convert a flat album name (e.g. 'Vacances 2026 Italie') to a nested relative path.

        Splits the album name using `self.album_delimiter` (default: space `' '`).
        Also normalizes any explicit `/` separators into path components.
        """
        normalized = album_name.replace("/", self.album_delimiter)
        parts = [
            part.strip()
            for part in normalized.split(self.album_delimiter)
            if part.strip() and part.strip() not in {".", ".."}
        ]
        if not parts:
            return Path(album_name.strip() or "Untitled")
        return Path(*parts)

    def relpath_to_album(self, rel_dir: Path) -> Optional[str]:
        """Convert a relative directory path under `albums/` into a flat album name.

        Joins folder levels with `self.album_delimiter` (default: space `' '`):
        e.g. `Path('Vacances/2026/Italie')` -> `'Vacances 2026 Italie'`.
        """
        if not rel_dir.parts or rel_dir == Path("."):
            return None
        return self.album_delimiter.join(rel_dir.parts)

    @staticmethod
    def compute_sha256(file_path: Path) -> str:
        """Compute the SHA-256 hex digest of a local file."""
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def compute_hashes(file_path: Path) -> tuple[str, str]:
        """Compute both (sha256_hex, sha512_base64) of a local file in a single pass."""
        sha256_hex, sha512_b64, _ = LocalProvider.compute_all_hashes(file_path)
        return sha256_hex, sha512_b64

    @staticmethod
    def compute_all_hashes(file_path: Path) -> tuple[str, str, str]:
        """Compute (sha256_hex, sha512_base64, sha1_base64) of a local file in a single pass."""
        h256 = hashlib.sha256()
        h512 = hashlib.sha512()
        h1 = hashlib.sha1()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                h256.update(chunk)
                h512.update(chunk)
                h1.update(chunk)
        return (
            h256.hexdigest(),
            base64.b64encode(h512.digest()).decode("ascii"),
            base64.b64encode(h1.digest()).decode("ascii"),
        )

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
            # Check Ente (metadata/<filename>.json) or Google Takeout (<filename>.json) sidecar metadata
            sidecar_candidates = [
                file_path.parent / "metadata" / f"{file_path.name}.json",
                file_path.with_name(f"{file_path.name}.json"),
                file_path.with_name(f"{file_path.name}.supplemental-metadata.json"),
            ]
            for sidecar in sidecar_candidates:
                if sidecar.is_file():
                    try:
                        meta_json = json.loads(sidecar.read_text(encoding="utf-8"))
                        ts_info = (
                            meta_json.get("photoTakenTime")
                            or meta_json.get("creationTime")
                            or {}
                        )
                        ts_val = (
                            ts_info.get("timestamp")
                            if isinstance(ts_info, dict)
                            else ts_info
                        )
                        if ts_val is not None:
                            ts_float = float(ts_val)
                            if ts_float > 1e12:
                                ts_float /= 1_000_000.0
                            created_at = datetime.fromtimestamp(ts_float)
                            break
                    except Exception:
                        pass

        if created_at is None:
            stat = file_path.stat()
            created_at = datetime.fromtimestamp(stat.st_mtime)

        return width, height, created_at, exif_summary

    def _extract_album_from_path(self, file_path: Path) -> Optional[str]:
        """Determine the album name from a file's location relative to `root_dir`."""
        rel_path = file_path.relative_to(self.root_dir)
        parts = rel_path.parts

        # Files inside `_library/...` are canonical storage, not album links
        if parts[0] == self.library_dirname:
            return None

        # Ignore Ente metadata sidecar folders if any image happens to be inside
        if "metadata" in parts[:-1]:
            return None

        # Files inside `albums/<level1>/<level2>/<file>`
        if parts[0] == self.albums_dirname:
            if len(parts) <= 2:
                return None
            album_rel_dir = Path(*parts[1:-1])
            return self.relpath_to_album(album_rel_dir)

        # Fallback for direct folder structures without `albums/` prefix (e.g. Ente export folders)
        if len(parts) > 1:
            return self.relpath_to_album(Path(*parts[:-1]))
        return None

    def _build_photo_item(
        self,
        canonical_path: Path,
        albums: list[str],
        checksum: str,
        all_paths: Optional[list[Path]] = None,
    ) -> PhotoItem:
        stat = canonical_path.stat()
        width, height, created_at, exif_meta = self.extract_image_metadata(canonical_path)
        mime_type, _ = mimetypes.guess_type(canonical_path.name)
        rel_path = canonical_path.relative_to(self.root_dir)
        sorted_albums = sorted(dict.fromkeys(albums))
        if all_paths is not None:
            exif_meta = {
                **exif_meta,
                "_all_paths": [str(p) for p in all_paths],
            }

        return PhotoItem(
            id=str(rel_path),
            filename=canonical_path.name,
            platform=self.platform_name,
            checksum=checksum,
            created_at=created_at,
            size=stat.st_size,
            width=width,
            height=height,
            mime_type=mime_type or "image/jpeg",
            album=sorted_albums[0] if sorted_albums else None,
            albums=sorted_albums,
            local_path=canonical_path,
            raw_metadata=exif_meta,
        )

    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """Yield deduplicated PhotoItem entries from the local directory.

        Files linked across `_library/YYYY/MM/` and multiple `albums/...` folders
        (via hardlinks, symlinks, or identical SHA-256 checksums) are grouped into
        a single `PhotoItem` carrying all its `albums`.

        When `self.albums_only` is True, scans only `albums/` (ignoring unlinked
        files in `_library/`, hidden directories such as `.meta`, and `metadata/`).
        """
        if not self.root_dir.exists():
            return

        scan_root = self.albums_root if self.albums_only else self.root_dir
        if not scan_root.exists():
            return

        def _iter_media_paths() -> Iterable[Path]:
            if not self.recursive:
                for p in sorted(scan_root.glob("*")):
                    if (p.is_file() or p.is_symlink()) and p.suffix.lower() in SUPPORTED_EXTENSIONS:
                        yield p
                return

            for walk_root, dirnames, filenames in os.walk(scan_root, topdown=True):
                dirnames[:] = sorted(
                    d for d in dirnames if not d.startswith(".") and d != "metadata"
                )
                rp = Path(walk_root)
                for fname in sorted(filenames):
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in SUPPORTED_EXTENSIONS:
                        yield rp / fname

        inode_to_hashes: dict[tuple[int, int], tuple[str, str, str]] = {}
        checksum_to_extra_hashes: dict[str, tuple[str, str]] = {}
        grouped_paths: dict[str, list[Path]] = {}
        scanned_files = 0

        for path in _iter_media_paths():
            if not (path.is_file() or path.is_symlink()):
                continue
            if not path.exists():
                # Skip broken symlinks
                continue

            scanned_files += 1
            if scanned_files % 1000 == 0:
                self._log(
                    f"[Local] Scanned {scanned_files} media file(s) ({len(grouped_paths)} unique)..."
                )

            resolved = path.resolve()
            st = resolved.stat()
            inode_key = (st.st_dev, st.st_ino)
            hashes = inode_to_hashes.get(inode_key)
            if hashes is None:
                hashes = self.compute_all_hashes(resolved)
                inode_to_hashes[inode_key] = hashes
            checksum, sha512_b64, sha1_b64 = hashes
            checksum_to_extra_hashes[checksum] = (sha512_b64, sha1_b64)

            grouped_paths.setdefault(checksum, []).append(path)

        for checksum, paths in grouped_paths.items():
            albums: list[str] = []
            canonical_path = paths[0]

            for p in paths:
                rel_parts = p.relative_to(self.root_dir).parts
                if rel_parts and rel_parts[0] == self.library_dirname and not p.is_symlink():
                    canonical_path = p
                alb = self._extract_album_from_path(p)
                if alb and alb not in albums:
                    albums.append(alb)

            if canonical_path.is_symlink():
                target = canonical_path.resolve()
                if target.is_relative_to(self.root_dir):
                    canonical_path = target

            if self.albums_only and not albums:
                continue

            if album and album not in albums:
                continue

            item = self._build_photo_item(canonical_path, albums, checksum, all_paths=paths)
            sha512_b64, sha1_b64 = checksum_to_extra_hashes[checksum]
            item.raw_metadata["sha512_b64"] = sha512_b64
            item.raw_metadata["sha1_b64"] = sha1_b64
            yield item

    def _canonical_library_path(self, filename: str, created_at: Optional[datetime]) -> Path:
        """Compute the canonical `_library/YYYY/MM/<filename>` path for a photo."""
        if not self.use_library_layout:
            return self.root_dir / filename
        dt = created_at or datetime.now()
        return self.library_root / f"{dt.year:04d}" / f"{dt.month:02d}" / filename

    def _create_album_link(self, canonical_path: Path, album_name: str) -> Path:
        """Create a hardlink, relative symlink, or copy inside `albums/<hierarchy>/<filename>`."""
        album_rel_dir = self.album_to_relpath(album_name)
        if self.use_library_layout:
            album_dir = self.albums_root / album_rel_dir
        else:
            album_dir = self.root_dir / album_rel_dir

        album_dir.mkdir(parents=True, exist_ok=True)
        link_path = album_dir / canonical_path.name

        if link_path.exists() or link_path.is_symlink():
            if not link_path.is_symlink() and link_path.resolve() == canonical_path.resolve():
                return link_path
            if (
                not link_path.is_symlink()
                and link_path.stat().st_ino == canonical_path.stat().st_ino
            ):
                return link_path
            link_path.unlink()

        if self.link_mode == "hardlink":
            try:
                os.link(canonical_path, link_path)
                return link_path
            except OSError:
                # Fallback to relative symlink if cross-device or unsupported
                rel_target = os.path.relpath(canonical_path, start=album_dir)
                os.symlink(rel_target, link_path)
                return link_path

        if self.link_mode == "symlink":
            rel_target = os.path.relpath(canonical_path, start=album_dir)
            os.symlink(rel_target, link_path)
            return link_path

        shutil.copy2(canonical_path, link_path)
        return link_path

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
        """Store a photo once in `_library/YYYY/MM/` and link it into all its album subfolders."""
        file_path = Path(file_path).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"Source file does not exist: {file_path}")

        target_filename = item.filename if item and item.filename else file_path.name
        _, _, extracted_dt, _ = self.extract_image_metadata(file_path)
        created_at = (item.created_at if item and item.created_at else None) or extracted_dt

        target_albums: list[str] = []
        if item:
            for a in item.albums:
                if a and a not in target_albums:
                    target_albums.append(a)
            if item.album and item.album not in target_albums:
                target_albums.append(item.album)
        if album and album not in target_albums:
            target_albums.append(album)

        if self.use_library_layout or not target_albums:
            canonical_path = self._canonical_library_path(target_filename, created_at)
        else:
            first_rel = self.album_to_relpath(target_albums[0])
            canonical_path = self.root_dir / first_rel / target_filename

        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        if file_path.resolve() != canonical_path.resolve():
            if self.link_mode == "hardlink":
                try:
                    if canonical_path.exists():
                        canonical_path.unlink()
                    os.link(file_path, canonical_path)
                except OSError:
                    shutil.copy2(file_path, canonical_path)
            else:
                shutil.copy2(file_path, canonical_path)
            self._log(
                f"[Local] Stored canonical file: {canonical_path.relative_to(self.root_dir)}"
            )

        if created_at:
            ts = created_at.timestamp()
            os.utime(canonical_path, (ts, ts))

        for alb in target_albums:
            link_path = self._create_album_link(canonical_path, alb)
            self._log(
                f"[Local] Linked ({self.link_mode}): {link_path.relative_to(self.root_dir)} -> "
                f"{canonical_path.relative_to(self.root_dir)}"
            )

        checksum = (
            item.checksum
            if item and item.checksum
            else self.compute_sha256(canonical_path)
        )
        return self._build_photo_item(canonical_path, target_albums, checksum)

    def needs_organization(self, item: PhotoItem, desired_albums: Iterable[str]) -> bool:
        """Return True if an existing photo needs canonical _library placement, album links, or inode deduplication."""
        source_path = item.local_path or (self.root_dir / item.id)
        if not source_path.exists():
            return False

        expected_canonical = self._canonical_library_path(item.filename, item.created_at)
        if self.use_library_layout and not expected_canonical.exists():
            return True

        canonical_ino = (
            expected_canonical.stat().st_ino
            if expected_canonical.exists()
            else source_path.stat().st_ino
        )

        all_albums = set(item.albums) | {a for a in desired_albums if a}
        for alb in all_albums:
            album_rel_dir = self.album_to_relpath(alb)
            album_dir = (
                self.albums_root / album_rel_dir
                if self.use_library_layout
                else self.root_dir / album_rel_dir
            )
            link_path = album_dir / item.filename
            if not (link_path.exists() or link_path.is_symlink()):
                return True
            if (
                self.link_mode == "hardlink"
                and not link_path.is_symlink()
                and link_path.stat().st_ino != canonical_ino
            ):
                return True

        # Check if multiple physical copies exist (e.g. exported by Ente into several flat album folders)
        if self.link_mode == "hardlink":
            raw_paths = item.raw_metadata.get("_all_paths", [])
            for p_str in raw_paths:
                p = Path(p_str)
                if p.exists() and not p.is_symlink() and p.stat().st_ino != canonical_ino:
                    return True

        return False

    def sync_albums(self, item: PhotoItem, albums: Iterable[str]) -> PhotoItem:
        """Ensure canonical _library file exists, create hierarchical album links, and deduplicate inodes."""
        source_path = item.local_path or (self.root_dir / item.id)
        if not source_path.exists():
            raise FileNotFoundError(f"Local file not found: {source_path}")

        canonical_path = self._canonical_library_path(item.filename, item.created_at)
        if self.use_library_layout and canonical_path.resolve() != source_path.resolve():
            canonical_path.parent.mkdir(parents=True, exist_ok=True)
            if not canonical_path.exists():
                if self.link_mode == "hardlink":
                    try:
                        os.link(source_path, canonical_path)
                    except OSError:
                        shutil.copy2(source_path, canonical_path)
                else:
                    shutil.copy2(source_path, canonical_path)
                self._log(
                    f"[Local] Linked canonical file: {canonical_path.relative_to(self.root_dir)}"
                )
            item.local_path = canonical_path
            item.id = str(canonical_path.relative_to(self.root_dir))
        else:
            canonical_path = source_path

        all_albums = list(dict.fromkeys([*item.albums, *(a for a in albums if a)]))
        for alb in all_albums:
            link_path = self._create_album_link(canonical_path, alb)
            item.add_album(alb)
            self._log(
                f"[Local] Linked ({self.link_mode}): {link_path.relative_to(self.root_dir)} -> "
                f"{canonical_path.relative_to(self.root_dir)}"
            )

        # Replace any duplicate physical copies (e.g. in flat Ente export folders) with hardlinks to canonical_path
        if self.link_mode == "hardlink":
            canonical_ino = canonical_path.stat().st_ino
            for p_str in item.raw_metadata.get("_all_paths", []):
                p = Path(p_str)
                if (
                    p.exists()
                    and not p.is_symlink()
                    and p.resolve() != canonical_path.resolve()
                    and p.stat().st_ino != canonical_ino
                ):
                    try:
                        p.unlink()
                        os.link(canonical_path, p)
                        self._log(
                            f"[Local] Deduplicated in-place with hardlink: {p.relative_to(self.root_dir)}"
                        )
                    except OSError:
                        pass

        return item
