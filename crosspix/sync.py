"""Core synchronization engine for transferring and deduplicating photos across platforms."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from typing import Callable, Optional

from crosspix.base import PhotoItem, PhotoProvider


def _default_progress(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class SyncResult:
    """Summary of a synchronization run between two photo providers."""

    source_platform: str
    target_platform: str
    scanned_source: int = 0
    scanned_target: int = 0
    copied: list[PhotoItem] = field(default_factory=list)
    albums_updated: list[PhotoItem] = field(default_factory=list)
    skipped: list[PhotoItem] = field(default_factory=list)
    errors: list[tuple[PhotoItem, str]] = field(default_factory=list)


class PhotoSynchronizer:
    """Synchronizes photos across LocalProvider, GooglePhotosProvider, EnteProvider, and ImmichProvider."""

    def __init__(
        self,
        dry_run: bool = False,
        on_progress: Optional[Callable[[str], None]] = _default_progress,
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
        """Perform a one-way synchronization from source provider to target provider.

        If a photo already exists on `target` (matched by checksum or filename/timestamp)
        but belongs to additional albums on `source`, `target.sync_albums(...)` is called
        to link the existing photo into the new albums without re-downloading or
        duplicating the file.
        """
        self._log(f"[Sync] Connecting to source ({source.platform_name})...")
        source.connect()
        self._log(f"[Sync] Connecting to target ({target.platform_name})...")
        target.connect()

        self._log(f"[Sync] Listing photos from source ({source.platform_name})...")
        source_items = list(source.list_photos(album=album))
        self._log(
            f"[Sync] Source ({source.platform_name}) scan complete: {len(source_items)} unique photo(s)."
        )

        self._log(f"[Sync] Listing photos from target ({target.platform_name})...")
        target_items = list(target.list_photos(album=album))
        self._log(
            f"[Sync] Target ({target.platform_name}) scan complete: {len(target_items)} unique photo(s)."
        )

        result = SyncResult(
            source_platform=source.platform_name,
            target_platform=target.platform_name,
            scanned_source=len(source_items),
            scanned_target=len(target_items),
        )

        total = len(source_items)
        self._log(
            f"[Sync] Starting synchronization: {source.platform_name} ({total} items) -> "
            f"{target.platform_name} ({len(target_items)} items)"
        )

        with tempfile.TemporaryDirectory(prefix="crosspix_sync_") as tmp_dir:
            staging_dir = Path(tmp_dir)
            for idx, src_item in enumerate(source_items, start=1):
                prefix = f"[{idx}/{total}]"
                matched_dst = next(
                    (dst_item for dst_item in target_items if src_item.matches(dst_item)),
                    None,
                )
                if matched_dst is not None:
                    missing_albums = [
                        alb for alb in src_item.albums if alb and alb not in matched_dst.albums
                    ]
                    needs_org_fn = getattr(target, "needs_organization", None)
                    needs_org = (
                        bool(needs_org_fn(matched_dst, src_item.albums))
                        if callable(needs_org_fn)
                        else False
                    )
                    if missing_albums or needs_org:
                        albums_to_sync = missing_albums or src_item.albums
                        if self.dry_run:
                            result.albums_updated.append(matched_dst)
                            self._log(
                                f"{prefix} [DRY-RUN] Would organize/link existing photo '{src_item.filename}' "
                                f"for albums: {albums_to_sync}"
                            )
                        else:
                            try:
                                target.sync_albums(matched_dst, albums_to_sync)
                                result.albums_updated.append(matched_dst)
                                self._log(
                                    f"{prefix} Organized/linked existing photo '{src_item.filename}' "
                                    f"for albums: {albums_to_sync}"
                                )
                            except Exception as exc:
                                result.errors.append((src_item, str(exc)))
                                self._log(
                                    f"{prefix} Error updating albums for '{src_item.filename}': {exc}"
                                )
                    else:
                        result.skipped.append(src_item)
                        self._log(f"{prefix} Skipping existing photo: {src_item.filename}")
                    continue

                if self.dry_run:
                    result.copied.append(src_item)
                    self._log(
                        f"{prefix} [DRY-RUN] Would copy: {src_item.filename} (albums={src_item.albums})"
                    )
                    continue

                try:
                    if src_item.local_path and src_item.local_path.exists():
                        local_file = src_item.local_path
                    else:
                        self._log(
                            f"{prefix} Downloading '{src_item.filename}' from {source.platform_name}..."
                        )
                        local_file = source.download_photo(src_item, staging_dir)

                    self._log(
                        f"{prefix} Saving '{src_item.filename}' to {target.platform_name} "
                        f"(albums={src_item.albums})..."
                    )
                    uploaded_item = target.upload_photo(
                        local_file,
                        item=src_item,
                        album=album or src_item.album,
                    )
                    result.copied.append(uploaded_item)
                    target_items.append(uploaded_item)
                    self._log(
                        f"{prefix} Synced '{src_item.filename}' -> {target.platform_name}"
                    )
                except Exception as exc:
                    result.errors.append((src_item, str(exc)))
                    self._log(f"{prefix} Error syncing '{src_item.filename}': {exc}")

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
