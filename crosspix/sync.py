"""Core synchronization engine for transferring and deduplicating photos across platforms."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from typing import Callable, Optional

from crosspix.base import PhotoItem, PhotoProvider


@dataclass
class SyncResult:
    """Summary of a synchronization run between two photo providers."""

    source_platform: str
    target_platform: str
    scanned_source: int = 0
    scanned_target: int = 0
    copied: list[PhotoItem] = field(default_factory=list)
    skipped: list[PhotoItem] = field(default_factory=list)
    errors: list[tuple[PhotoItem, str]] = field(default_factory=list)


class PhotoSynchronizer:
    """Synchronizes photos across LocalProvider, GooglePhotosProvider, EnteProvider, and ImmichProvider."""

    def __init__(
        self,
        dry_run: bool = False,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.dry_run = dry_run
        self.on_progress = on_progress

    def _log(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    def sync(
        self,
        source: PhotoProvider,
        target: PhotoProvider,
        album: Optional[str] = None,
    ) -> SyncResult:
        """Perform a one-way synchronization from source provider to target provider."""
        source.connect()
        target.connect()

        source_items = list(source.list_photos(album=album))
        target_items = list(target.list_photos(album=album))

        result = SyncResult(
            source_platform=source.platform_name,
            target_platform=target.platform_name,
            scanned_source=len(source_items),
            scanned_target=len(target_items),
        )

        self._log(
            f"Syncing {source.platform_name} ({len(source_items)} items) -> "
            f"{target.platform_name} ({len(target_items)} items)"
        )

        with tempfile.TemporaryDirectory(prefix="crosspix_sync_") as tmp_dir:
            staging_dir = Path(tmp_dir)
            for src_item in source_items:
                already_exists = any(src_item.matches(dst_item) for dst_item in target_items)
                if already_exists:
                    result.skipped.append(src_item)
                    self._log(f"Skipping existing photo: {src_item.filename}")
                    continue

                if self.dry_run:
                    result.copied.append(src_item)
                    self._log(f"[DRY-RUN] Would copy: {src_item.filename}")
                    continue

                try:
                    if src_item.local_path and src_item.local_path.exists():
                        local_file = src_item.local_path
                    else:
                        local_file = source.download_photo(src_item, staging_dir)

                    uploaded_item = target.upload_photo(
                        local_file,
                        item=src_item,
                        album=album or src_item.album,
                    )
                    result.copied.append(uploaded_item)
                    target_items.append(uploaded_item)
                    self._log(f"Copied: {src_item.filename} -> {target.platform_name}")
                except Exception as exc:
                    result.errors.append((src_item, str(exc)))
                    self._log(f"Error syncing {src_item.filename}: {exc}")

        return result

    def bisync(
        self,
        provider_a: PhotoProvider,
        provider_b: PhotoProvider,
        album: Optional[str] = None,
    ) -> tuple[SyncResult, SyncResult]:
        """Perform a bi-directional synchronization between provider_a and provider_b."""
        forward = self.sync(provider_a, provider_b, album=album)
        backward = self.sync(provider_b, provider_a, album=album)
        return forward, backward
