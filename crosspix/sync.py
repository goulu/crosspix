"""Core synchronization engine for transferring and deduplicating photos across platforms."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import json
import mimetypes
from pathlib import Path
import tempfile
from typing import Any, Callable, Optional

from crosspix.base import (
    PhotoItem,
    PhotoProvider,
    is_video_filename,
    is_video_item,
)
from crosspix.youtube import is_youtube_rate_limit_error


def _default_progress(msg: str) -> None:
    print(msg, flush=True)


BYTES_PER_MB = 1024 * 1024


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
    skipped_large_videos: list[PhotoItem] = field(default_factory=list)
    routed_to_youtube: list[PhotoItem] = field(default_factory=list)
    errors: list[tuple[PhotoItem, str]] = field(default_factory=list)


class PhotoSynchronizer:
    """Synchronizes photos across LocalProvider, GooglePhotosProvider, EnteProvider, ImmichProvider, and YouTubeProvider."""

    def __init__(
        self,
        dry_run: bool = False,
        on_progress: Optional[Callable[[str], None]] = _default_progress,
        max_video_size_mb: Optional[float] = None,
        min_video_size_mb: Optional[float] = None,
        large_video_target: Optional[PhotoProvider] = None,
        sync_albums: bool = True,
        images_only: bool = False,
        error_log_path: Optional[str | Path] = None,
        largest_first: bool = False,
        video_index_path: Optional[str | Path] = None,
        refresh_video_index: bool = False,
    ) -> None:
        self.dry_run = dry_run
        self.on_progress = on_progress
        self.max_video_size_mb = (
            max_video_size_mb if (max_video_size_mb is not None and max_video_size_mb > 0) else None
        )
        self.min_video_size_mb = (
            min_video_size_mb if (min_video_size_mb is not None and min_video_size_mb > 0) else None
        )
        self.large_video_target = large_video_target
        self.sync_albums = sync_albums
        self.images_only = images_only
        self.error_log_path = Path(error_log_path).expanduser() if error_log_path else None
        self.largest_first = largest_first
        self.video_index_path = Path(video_index_path).expanduser() if video_index_path else None
        self.refresh_video_index = refresh_video_index
        self._large_video_target_connected: bool = False

    def _record_error(
        self,
        result: SyncResult,
        item: PhotoItem,
        context: str,
        exc: Exception,
    ) -> None:
        """Record an error on `result`, log it to console, and append it to `error_log_path`."""
        err_msg = str(exc)
        result.errors.append((item, err_msg))
        self._log(f"  Error ({context}) '{item.filename}': {err_msg}")
        if self.error_log_path is not None:
            try:
                self.error_log_path.parent.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ref = item.local_path or item.id or item.filename
                with self.error_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"[{ts}] {result.source_platform}->{result.target_platform} "
                        f"| {context} | {ref} | {err_msg}\n"
                    )
            except OSError:
                pass

    def _max_video_bytes(self) -> Optional[int]:
        if self.max_video_size_mb is None:
            return None
        return int(self.max_video_size_mb * BYTES_PER_MB)

    def _min_video_bytes(self) -> Optional[int]:
        if self.min_video_size_mb is None:
            return None
        return int(self.min_video_size_mb * BYTES_PER_MB)

    def _ensure_large_video_target_ready(self) -> Any:
        yt = self.large_video_target
        if yt is not None and not self._large_video_target_connected:
            self._log(f"[Sync] Connecting to large-video target ({yt.platform_name})...")
            yt.connect()
            if callable(getattr(yt, "prefetch_uploaded_videos", None)):
                yt.prefetch_uploaded_videos()
            if callable(getattr(yt, "prefetch_playlists", None)):
                yt.prefetch_playlists()
            self._large_video_target_connected = True
        return yt

    def _route_local_video_to_youtube(
        self,
        fpath: Path,
        resolved: Path,
        size_bytes: int,
        ino_key: Optional[tuple[int, int]],
        album_name: Optional[str],
        source: Any,
        yt_target: Any,
        inode_to_yt: dict[tuple[int, int], tuple[PhotoItem, str]],
        result: SyncResult,
    ) -> str:
        """Upload or playlist-link a local video to a YouTubeProvider. Returns 'uploaded'|'linked'|'skipped'|'error'."""
        size_mb = size_bytes / BYTES_PER_MB
        try:
            cached = inode_to_yt.get(ino_key) if ino_key is not None else None
            if cached is not None:
                existing_yt, sha1_hex = cached
            else:
                sha1_hex = source.compute_sha1_hex(resolved)
                existing_yt = (
                    yt_target.find_existing_video(sha1_hex, fpath.name)
                    if callable(getattr(yt_target, "find_existing_video", None))
                    else None
                )
                if existing_yt is not None and ino_key is not None:
                    inode_to_yt[ino_key] = (existing_yt, sha1_hex)

            if existing_yt is not None:
                if album_name:
                    pl_id = yt_target._resolve_playlist_id(album_name, create_if_missing=False)
                    already_in_pl = (
                        existing_yt.id in yt_target.get_playlist_video_ids(pl_id)
                        if pl_id
                        else False
                    )
                    if not already_in_pl:
                        if self.dry_run:
                            result.albums_updated.append(existing_yt)
                            self._log(
                                f"  [DRY-RUN] [YouTube] Would add existing video '{fpath.name}' ({size_mb:.1f} MB) -> playlist '{album_name}'"
                            )
                            return "linked"
                        yt_target.add_video_to_playlist(existing_yt.id, album_name)
                        existing_yt.add_album(album_name)
                        result.albums_updated.append(existing_yt)
                        self._log(
                            f"  [YouTube] Linked existing video '{fpath.name}' -> playlist '{album_name}'"
                        )
                        return "linked"
                result.skipped.append(existing_yt)
                self._log(f"  [YouTube] Skipping existing video '{fpath.name}' ({size_mb:.1f} MB)")
                return "skipped"
        except Exception as exc:
            dummy = PhotoItem(
                id=str(fpath),
                filename=fpath.name,
                platform=source.platform_name,
                local_path=fpath,
            )
            self._record_error(result, dummy, "youtube check/playlist", exc)
            if is_youtube_rate_limit_error(exc):
                return "rate_limited"
            return "error"

        try:
            mime_type, _ = mimetypes.guess_type(fpath.name)
            _, _, created_at, _ = source.extract_image_metadata(fpath)
            src_item = PhotoItem(
                id=str(fpath),
                filename=fpath.name,
                platform=source.platform_name,
                checksum=sha1_hex,
                created_at=created_at or datetime.fromtimestamp(resolved.stat().st_mtime),
                size=size_bytes,
                mime_type=mime_type or "video/mp4",
                album=album_name,
                albums=[album_name] if album_name else [],
                local_path=resolved,
                raw_metadata={"sha1_hex": sha1_hex},
            )
        except Exception as exc:
            dummy = PhotoItem(
                id=str(fpath),
                filename=fpath.name,
                platform=source.platform_name,
                local_path=fpath,
            )
            self._record_error(result, dummy, "youtube metadata", exc)
            return "error"

        if self.dry_run:
            result.routed_to_youtube.append(src_item)
            self._log(
                f"  [DRY-RUN] [YouTube] Would upload video '{fpath.name}' ({size_mb:.1f} MB)"
                + (f" -> playlist '{album_name}'" if album_name else "")
            )
            return "uploaded"

        try:
            self._log(
                f"  [YouTube] Uploading video '{fpath.name}' ({size_mb:.1f} MB)"
                + (f" -> playlist '{album_name}'" if album_name else "")
                + "..."
            )
            uploaded = yt_target.upload_photo(resolved, item=src_item, album=album_name)
            if ino_key is not None:
                inode_to_yt[ino_key] = (uploaded, sha1_hex)
            result.routed_to_youtube.append(uploaded)
            self._log(
                f"  [YouTube] Uploaded '{fpath.name}' (videoId={uploaded.id})"
                + (f" -> playlist '{album_name}'" if album_name else "")
            )
            return "uploaded"
        except Exception as exc:
            self._record_error(result, src_item, "youtube upload", exc)
            if is_youtube_rate_limit_error(exc):
                return "rate_limited"
            return "error"

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

        # Use fast resumable streaming sync when transferring local folders -> Immich
        if (
            callable(getattr(source, "iter_streaming_folders", None))
            and callable(getattr(target, "check_bulk_upload", None))
            and callable(getattr(target, "prefetch_albums", None))
        ):
            return self._sync_streaming_to_immich(source, target, album=album)

        # Use fast resumable streaming sync when transferring local folders -> YouTube
        if (
            callable(getattr(source, "iter_streaming_folders", None))
            and callable(getattr(target, "find_existing_video", None))
            and callable(getattr(target, "prefetch_playlists", None))
        ):
            return self._sync_streaming_to_youtube(source, target, album=album)

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
        if target.platform_name == "youtube" and self.largest_first:
            source_items.sort(key=lambda it: -(it.size or 0))

        # Build O(1) lookup indexes over target_items (hash + lowercase filename)
        dst_by_hash: dict[str, PhotoItem] = {}
        dst_by_name: dict[str, list[PhotoItem]] = {}

        def _index_target(dst_item: PhotoItem) -> None:
            for h in (
                dst_item.checksum,
                dst_item.raw_metadata.get("sha512_b64"),
                dst_item.raw_metadata.get("sha1_b64"),
            ):
                if isinstance(h, str) and h:
                    dst_by_hash.setdefault(h.lower(), dst_item)
            dst_by_name.setdefault(dst_item.filename.lower(), []).append(dst_item)

        for dst_item in target_items:
            _index_target(dst_item)

        def _find_match(src_item: PhotoItem) -> Optional[PhotoItem]:
            for h in (
                src_item.checksum,
                src_item.raw_metadata.get("sha512_b64"),
                src_item.raw_metadata.get("sha1_b64"),
            ):
                if isinstance(h, str) and h:
                    hit = dst_by_hash.get(h.lower())
                    if hit is not None:
                        return hit
            for cand in dst_by_name.get(src_item.filename.lower(), ()):
                if src_item.matches(cand):
                    return cand
            return None

        with tempfile.TemporaryDirectory(prefix="crosspix_sync_") as tmp_dir:
            staging_dir = Path(tmp_dir)
            max_vid_bytes = self._max_video_bytes()
            min_vid_bytes = self._min_video_bytes()
            for idx, src_item in enumerate(source_items, start=1):
                prefix = f"[{idx}/{total}]"

                # Enforce video-only / video size filters
                is_vid = is_video_item(src_item)
                item_size = src_item.size
                if item_size is None and src_item.local_path and src_item.local_path.exists():
                    try:
                        item_size = src_item.local_path.stat().st_size
                    except OSError:
                        item_size = None

                if self.images_only and is_vid:
                    result.skipped.append(src_item)
                    continue

                if target.platform_name == "youtube" and not is_vid:
                    result.skipped.append(src_item)
                    continue

                if min_vid_bytes is not None:
                    if not is_vid or (item_size is not None and item_size < min_vid_bytes):
                        result.skipped.append(src_item)
                        continue

                if is_vid and max_vid_bytes is not None and item_size is not None and item_size > max_vid_bytes:
                    size_mb = item_size / BYTES_PER_MB
                    if self.large_video_target is not None:
                        yt = self._ensure_large_video_target_ready()
                        local_file = (
                            src_item.local_path
                            if (src_item.local_path and src_item.local_path.exists())
                            else source.download_photo(src_item, staging_dir)
                        )
                        if self.dry_run:
                            result.routed_to_youtube.append(src_item)
                            self._log(
                                f"{prefix} [DRY-RUN] Would route large video '{src_item.filename}' "
                                f"({size_mb:.1f} MB > {self.max_video_size_mb:.1f} MB) -> {yt.platform_name}"
                            )
                        else:
                            try:
                                uploaded_yt = yt.upload_photo(
                                    local_file, item=src_item, album=album or src_item.album
                                )
                                result.routed_to_youtube.append(uploaded_yt)
                                self._log(
                                    f"{prefix} Routed large video '{src_item.filename}' "
                                    f"({size_mb:.1f} MB) -> {yt.platform_name}"
                                )
                            except Exception as exc:
                                self._record_error(result, src_item, f"route to {yt.platform_name}", exc)
                    else:
                        result.skipped_large_videos.append(src_item)
                        result.skipped.append(src_item)
                        self._log(
                            f"{prefix} Skipping large video '{src_item.filename}' "
                            f"({size_mb:.1f} MB > {self.max_video_size_mb:.1f} MB limit)"
                        )
                    continue

                matched_dst = _find_match(src_item)
                if matched_dst is not None:
                    if not self.sync_albums:
                        result.skipped.append(src_item)
                        self._log(f"{prefix} Skipping existing photo: {src_item.filename}")
                        continue
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
                                self._record_error(result, src_item, "album update", exc)
                    else:
                        result.skipped.append(src_item)
                        self._log(f"{prefix} Skipping existing photo: {src_item.filename}")
                    continue

                upload_item = (
                    src_item
                    if self.sync_albums
                    else replace(src_item, album=None, albums=[])
                )
                upload_album = (album or src_item.album) if self.sync_albums else None

                if self.dry_run:
                    result.copied.append(upload_item)
                    self._log(
                        f"{prefix} [DRY-RUN] Would copy: {src_item.filename} (albums={upload_item.albums})"
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
                        f"(albums={upload_item.albums})..."
                    )
                    uploaded_item = target.upload_photo(
                        local_file,
                        item=upload_item,
                        album=upload_album,
                    )
                    result.copied.append(uploaded_item)
                    target_items.append(uploaded_item)
                    _index_target(uploaded_item)
                    self._log(
                        f"{prefix} Synced '{src_item.filename}' -> {target.platform_name}"
                    )
                except Exception as exc:
                    self._record_error(result, src_item, "sync", exc)
                    if target.platform_name == "youtube" and is_youtube_rate_limit_error(exc):
                        self._log(
                            "[YouTube] Upload/rate limit reached (429 / uploadLimitExceeded / quota). "
                            "Stopping YouTube export immediately."
                        )
                        break

        return result

    def _sync_streaming_to_immich(
        self,
        source: Any,
        target: Any,
        album: Optional[str] = None,
        batch_size: int = 50,
    ) -> SyncResult:
        """Stream folder-by-folder from a local directory to Immich with instant resumability.

        Avoids scanning the entire local tree or listing all remote Immich assets upfront:
        - Fetches existing Immich album titles once (`GET /albums`).
        - Processes each local album folder in small batches using `POST /assets/bulk-upload-check`
          (SHA-1), immediately skipping already-uploaded & already-linked photos, linking
          existing assets into missing albums without re-uploading, and uploading only new files.
        - Re-running after an interruption resumes seamlessly where it left off.
        """
        existing_albums = target.prefetch_albums() if self.sync_albums else {}
        mode_hint = " (no albums)" if not self.sync_albums else f" ({len(existing_albums)} existing Immich album(s))"
        self._log(
            f"[Sync] Streaming {source.platform_name} -> {target.platform_name}"
            f"{mode_hint}, batch={batch_size}..."
        )

        result = SyncResult(
            source_platform=source.platform_name,
            target_platform=target.platform_name,
        )

        # Cache (st_dev, st_ino) -> (immich_asset_id, sha1_hex) for hardlinks shared across albums
        inode_to_asset: dict[tuple[int, int], tuple[str, str]] = {}
        inode_to_yt: dict[tuple[int, int], tuple[PhotoItem, str]] = {}
        youtube_rate_limited = False
        max_vid_bytes = self._max_video_bytes()
        min_vid_bytes = self._min_video_bytes()
        if max_vid_bytes is not None:
            action_desc = (
                f"routed to {self.large_video_target.platform_name}"
                if self.large_video_target is not None
                else "skipped"
            )
            self._log(
                f"[Sync] Video size limit active: videos > {self.max_video_size_mb:.1f} MB will be {action_desc}."
            )
        album_idx = 0

        for raw_album_name, file_paths in source.iter_streaming_folders(album=album):
            album_idx += 1
            album_name = raw_album_name if self.sync_albums else None
            label = album_name or (raw_album_name or "_library")
            total_in_folder = len(file_paths)
            self._log(
                f"[Folder {album_idx}] {label} ({total_in_folder} file(s))"
                + (" [no-albums]" if not self.sync_albums else "")
                + "..."
            )

            album_id: Optional[str] = None
            album_asset_ids: set[str] = set()
            if album_name:
                album_id = target._resolve_album_id(album_name, create_if_missing=False)
                if album_id:
                    try:
                        album_asset_ids = target.get_album_asset_ids(album_id)
                    except Exception as exc:
                        self._log(f"  Warning: could not fetch assets of album '{album_name}': {exc}")

            folder_copied = 0
            folder_linked = 0
            folder_skipped = 0
            folder_errors = 0

            for start in range(0, total_in_folder, batch_size):
                batch = file_paths[start : start + batch_size]

                # Step 1: resolve inode cache or compute SHA-1 for bulk-upload-check
                to_check: list[tuple[str, str]] = []
                batch_meta: list[tuple[Path, Optional[tuple[int, int]], Optional[str], Optional[str]]] = []
                # tuple: (path, inode_key, known_asset_id, sha1_hex)

                for offset, fpath in enumerate(batch):
                    result.scanned_source += 1
                    try:
                        resolved = fpath.resolve()
                        st = resolved.stat()
                        ino_key = (st.st_dev, st.st_ino)
                    except Exception as exc:
                        folder_errors += 1
                        dummy = PhotoItem(
                            id=str(fpath),
                            filename=fpath.name,
                            platform=source.platform_name,
                            local_path=fpath,
                        )
                        self._record_error(result, dummy, "read", exc)
                        continue

                    # Check images-only & video size filters BEFORE hashing or contacting Immich
                    is_vid = is_video_filename(fpath.name)
                    if self.images_only and is_vid:
                        folder_skipped += 1
                        continue
                    if min_vid_bytes is not None and (not is_vid or st.st_size < min_vid_bytes):
                        folder_skipped += 1
                        continue
                    if is_vid and max_vid_bytes is not None and st.st_size > max_vid_bytes:
                        size_mb = st.st_size / BYTES_PER_MB
                        if self.large_video_target is not None and not youtube_rate_limited:
                            yt = self._ensure_large_video_target_ready()
                            outcome = self._route_local_video_to_youtube(
                                fpath=fpath,
                                resolved=resolved,
                                size_bytes=st.st_size,
                                ino_key=ino_key,
                                album_name=album_name,
                                source=source,
                                yt_target=yt,
                                inode_to_yt=inode_to_yt,
                                result=result,
                            )
                            if outcome == "rate_limited":
                                youtube_rate_limited = True
                                folder_errors += 1
                                self._log(
                                    "[YouTube] Upload/rate limit reached (429 / uploadLimitExceeded / quota). "
                                    "Stopping YouTube uploads for remaining large videos."
                                )
                            elif outcome == "error":
                                folder_errors += 1
                            elif outcome == "linked":
                                folder_linked += 1
                            elif outcome == "skipped":
                                folder_skipped += 1
                        else:
                            skipped_item = PhotoItem(
                                id=str(fpath),
                                filename=fpath.name,
                                platform=source.platform_name,
                                size=st.st_size,
                                album=album_name,
                                albums=[album_name] if album_name else [],
                                local_path=resolved,
                            )
                            result.skipped_large_videos.append(skipped_item)
                            result.skipped.append(skipped_item)
                            folder_skipped += 1
                            self._log(
                                f"  Skipping large video '{fpath.name}' "
                                f"({size_mb:.1f} MB > {self.max_video_size_mb:.1f} MB limit)"
                            )
                        continue

                    cached = inode_to_asset.get(ino_key)
                    if cached is not None:
                        asset_id, sha1_hex = cached
                        batch_meta.append((fpath, ino_key, asset_id, sha1_hex))
                    else:
                        try:
                            sha1_hex = source.compute_sha1_hex(resolved)
                        except Exception as exc:
                            folder_errors += 1
                            dummy = PhotoItem(
                                id=str(fpath),
                                filename=fpath.name,
                                platform=source.platform_name,
                                local_path=fpath,
                            )
                            self._record_error(result, dummy, "hash", exc)
                            continue
                        cid = str(offset)
                        to_check.append((cid, sha1_hex))
                        batch_meta.append((fpath, ino_key, None, sha1_hex))

                # Step 2: bulk-upload-check against Immich (1 HTTP call per batch)
                check_results: dict[str, tuple[str, Optional[str]]] = {}
                if to_check:
                    try:
                        check_results = target.check_bulk_upload(to_check)
                    except Exception as exc:
                        self._log(f"  Warning: bulk-upload-check failed ({exc}); falling back to direct upload.")

                # Step 3: process each file in the batch (skip / link / upload)
                ids_to_link_in_album: list[tuple[str, PhotoItem]] = []
                check_cursor = 0

                for fpath, ino_key, known_asset_id, sha1_hex in batch_meta:
                    asset_id = known_asset_id
                    if asset_id is None:
                        cid = to_check[check_cursor][0]
                        check_cursor += 1
                        action, existing_id = check_results.get(cid, ("accept", None))
                        if action == "reject" and existing_id:
                            asset_id = existing_id
                            if ino_key is not None and sha1_hex is not None:
                                inode_to_asset[ino_key] = (asset_id, sha1_hex)

                    if asset_id is not None:
                        # Asset already exists on Immich!
                        existing_item = PhotoItem(
                            id=asset_id,
                            filename=fpath.name,
                            platform=target.platform_name,
                            checksum=sha1_hex,
                            album=album_name,
                            albums=[album_name] if album_name else [],
                        )
                        if album_name and asset_id not in album_asset_ids:
                            ids_to_link_in_album.append((asset_id, existing_item))
                            album_asset_ids.add(asset_id)
                        else:
                            folder_skipped += 1
                            result.skipped.append(existing_item)
                        continue

                    # New asset -> upload immediately!
                    try:
                        mime_type, _ = mimetypes.guess_type(fpath.name)
                        _, _, created_at, _ = source.extract_image_metadata(fpath)
                        src_item = PhotoItem(
                            id=str(fpath),
                            filename=fpath.name,
                            platform=source.platform_name,
                            checksum=sha1_hex,
                            created_at=created_at or datetime.fromtimestamp(fpath.stat().st_mtime),
                            mime_type=mime_type or "application/octet-stream",
                            album=album_name,
                            albums=[album_name] if album_name else [],
                            local_path=fpath,
                            raw_metadata={"sha1_hex": sha1_hex} if sha1_hex else {},
                        )
                    except Exception as exc:
                        folder_errors += 1
                        dummy = PhotoItem(
                            id=str(fpath),
                            filename=fpath.name,
                            platform=source.platform_name,
                            local_path=fpath,
                        )
                        self._record_error(result, dummy, "metadata", exc)
                        continue

                    if self.dry_run:
                        folder_copied += 1
                        result.copied.append(src_item)
                        self._log(f"  [DRY-RUN] Would upload '{fpath.name}' -> album '{label}'")
                        continue

                    try:
                        # Ensure album exists once before/after upload
                        uploaded = target.upload_photo(
                            fpath.resolve(),
                            item=src_item,
                            album=album_name,
                        )
                        if uploaded.id:
                            if ino_key is not None and sha1_hex is not None:
                                inode_to_asset[ino_key] = (uploaded.id, sha1_hex)
                            if album_name:
                                if album_id is None:
                                    album_id = target._resolve_album_id(album_name, create_if_missing=False)
                                album_asset_ids.add(uploaded.id)
                        folder_copied += 1
                        result.copied.append(uploaded)
                        self._log(
                            f"  [{folder_copied + folder_linked + folder_skipped + folder_errors}/{total_in_folder}] "
                            f"Uploaded '{fpath.name}' -> {label} "
                            f"(total: {len(result.copied)} uploaded, {len(result.albums_updated)} linked, {len(result.skipped)} skipped)"
                        )
                    except Exception as exc:
                        folder_errors += 1
                        self._record_error(result, src_item, "upload", exc)

                # Link any already-existing assets that weren't in this Immich album yet (1 PUT per batch!)
                if ids_to_link_in_album and album_name:
                    if self.dry_run:
                        for _, ex_item in ids_to_link_in_album:
                            folder_linked += 1
                            result.albums_updated.append(ex_item)
                        self._log(
                            f"  [DRY-RUN] Would link {len(ids_to_link_in_album)} existing photo(s) -> album '{album_name}'"
                        )
                    else:
                        try:
                            if album_id is None:
                                album_id = target._resolve_album_id(album_name, create_if_missing=True)
                            if album_id:
                                target.add_assets_to_album(
                                    album_id, [aid for aid, _ in ids_to_link_in_album]
                                )
                            for _, ex_item in ids_to_link_in_album:
                                folder_linked += 1
                                result.albums_updated.append(ex_item)
                            self._log(
                                f"  Linked {len(ids_to_link_in_album)} existing photo(s) -> album '{album_name}'"
                            )
                        except Exception as exc:
                            for _, ex_item in ids_to_link_in_album:
                                folder_errors += 1
                                self._record_error(result, ex_item, f"link album '{album_name}'", exc)

            self._log(
                f"[Album {album_idx}] Done '{label}': "
                f"{folder_copied} uploaded, {folder_linked} linked, "
                f"{folder_skipped} skipped, {folder_errors} errors "
                f"(cumulative: {len(result.copied)} uploaded, {len(result.albums_updated)} linked, "
                f"{len(result.skipped)} skipped, {len(result.errors)} errors)."
            )

        return result

    def _sync_streaming_to_youtube(
        self,
        source: Any,
        target: Any,
        album: Optional[str] = None,
    ) -> SyncResult:
        """Stream folder-by-folder from a local directory to YouTube (videos only, resumable)."""
        existing_videos = target.prefetch_uploaded_videos()
        existing_playlists = target.prefetch_playlists() if self.sync_albums else {}
        max_vid_bytes = self._max_video_bytes()
        min_vid_bytes = self._min_video_bytes()

        size_hint_parts = []
        if min_vid_bytes is not None:
            size_hint_parts.append(f">= {self.min_video_size_mb:.1f} MB")
        if max_vid_bytes is not None:
            size_hint_parts.append(f"<= {self.max_video_size_mb:.1f} MB")
        size_hint = f", filter: {' & '.join(size_hint_parts)}" if size_hint_parts else ""

        self._log(
            f"[Sync] Streaming videos {source.platform_name} -> {target.platform_name} "
            f"({len(existing_videos)} existing video(s), {len(existing_playlists)} playlist(s){size_hint})..."
        )

        result = SyncResult(
            source_platform=source.platform_name,
            target_platform=target.platform_name,
            scanned_target=len(existing_videos),
        )

        inode_to_yt: dict[tuple[int, int], tuple[PhotoItem, str]] = {}

        if not self.largest_first:
            # Default streaming mode: walk folder by folder immediately without upfront scan
            album_idx = 0
            for raw_album_name, file_paths in source.iter_streaming_folders(album=album):
                album_name = raw_album_name if self.sync_albums else None
                matching_videos: list[tuple[Path, Path, int, tuple[int, int]]] = []
                for fpath in file_paths:
                    if not is_video_filename(fpath.name):
                        continue
                    result.scanned_source += 1
                    try:
                        resolved = fpath.resolve()
                        st = resolved.stat()
                    except Exception as exc:
                        dummy = PhotoItem(
                            id=str(fpath),
                            filename=fpath.name,
                            platform=source.platform_name,
                            local_path=fpath,
                        )
                        self._record_error(result, dummy, "youtube read", exc)
                        continue
                    if min_vid_bytes is not None and st.st_size < min_vid_bytes:
                        continue
                    if max_vid_bytes is not None and st.st_size > max_vid_bytes:
                        skipped_item = PhotoItem(
                            id=str(fpath),
                            filename=fpath.name,
                            platform=source.platform_name,
                            size=st.st_size,
                            album=album_name,
                            albums=[album_name] if album_name else [],
                            local_path=resolved,
                        )
                        result.skipped_large_videos.append(skipped_item)
                        result.skipped.append(skipped_item)
                        continue
                    matching_videos.append((fpath, resolved, st.st_size, (st.st_dev, st.st_ino)))

                if not matching_videos:
                    continue

                album_idx += 1
                label = album_name or "_library"
                self._log(f"[Album {album_idx}] {label} ({len(matching_videos)} matching video(s))...")

                for fpath, resolved, size_bytes, ino_key in matching_videos:
                    before_yt = len(result.routed_to_youtube)
                    outcome = self._route_local_video_to_youtube(
                        fpath=fpath,
                        resolved=resolved,
                        size_bytes=size_bytes,
                        ino_key=ino_key,
                        album_name=album_name,
                        source=source,
                        yt_target=target,
                        inode_to_yt=inode_to_yt,
                        result=result,
                    )
                    if outcome == "uploaded" and len(result.routed_to_youtube) > before_yt:
                        result.copied.append(result.routed_to_youtube[-1])
                    elif outcome == "rate_limited":
                        self._log(
                            "[YouTube] Upload/rate limit reached (429 / uploadLimitExceeded / quota). "
                            "Stopping YouTube export immediately; re-run later to resume."
                        )
                        return result

            return result

        # --largest-first mode: use a persistent JSON index to avoid rescanning on subsequent runs
        index_path = (
            self.video_index_path
            if self.video_index_path is not None
            else (source.root_dir / ".crosspix_video_index.json")
        )
        index_meta = {
            "version": 1,
            "root": str(source.root_dir),
            "albums_only": bool(getattr(source, "albums_only", False)),
            "recursive": bool(getattr(source, "recursive", True)),
            "sync_albums": bool(self.sync_albums),
            "album_filter": album,
            "min_video_size_mb": self.min_video_size_mb,
            "max_video_size_mb": self.max_video_size_mb,
        }

        def _save_index(entries: list[dict[str, Any]]) -> None:
            payload = {
                **index_meta,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "videos": entries,
            }
            try:
                index_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_idx = index_path.with_suffix(index_path.suffix + ".tmp")
                tmp_idx.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp_idx.replace(index_path)
            except OSError as exc:
                self._log(f"[Sync] Warning: could not save video index '{index_path}': {exc}")

        index_entries: Optional[list[dict[str, Any]]] = None
        if not self.refresh_video_index and index_path.is_file():
            try:
                loaded = json.loads(index_path.read_text(encoding="utf-8"))
                if (
                    isinstance(loaded, dict)
                    and all(loaded.get(k) == v for k, v in index_meta.items())
                    and isinstance(loaded.get("videos"), list)
                ):
                    index_entries = loaded["videos"]
                    pending_cnt = sum(1 for e in index_entries if not e.get("done"))
                    self._log(
                        f"[Sync] Loaded cached video index '{index_path}': "
                        f"{len(index_entries)} video(s) ({pending_cnt} remaining)."
                    )
            except Exception as exc:
                self._log(f"[Sync] Warning: ignoring unreadable video index '{index_path}': {exc}")

        if index_entries is None:
            self._log(f"[Sync] Building video index for {source.root_dir}...")
            raw_entries: list[dict[str, Any]] = []
            for raw_album_name, file_paths in source.iter_streaming_folders(album=album):
                album_name = raw_album_name if self.sync_albums else None
                for fpath in file_paths:
                    if not is_video_filename(fpath.name):
                        continue
                    result.scanned_source += 1
                    try:
                        resolved = fpath.resolve()
                        st = resolved.stat()
                    except Exception as exc:
                        dummy = PhotoItem(
                            id=str(fpath),
                            filename=fpath.name,
                            platform=source.platform_name,
                            local_path=fpath,
                        )
                        self._record_error(result, dummy, "youtube read", exc)
                        continue
                    if min_vid_bytes is not None and st.st_size < min_vid_bytes:
                        continue
                    if max_vid_bytes is not None and st.st_size > max_vid_bytes:
                        skipped_item = PhotoItem(
                            id=str(fpath),
                            filename=fpath.name,
                            platform=source.platform_name,
                            size=st.st_size,
                            album=album_name,
                            albums=[album_name] if album_name else [],
                            local_path=resolved,
                        )
                        result.skipped_large_videos.append(skipped_item)
                        result.skipped.append(skipped_item)
                        continue
                    raw_entries.append(
                        {
                            "path": str(fpath),
                            "resolved": str(resolved),
                            "size": st.st_size,
                            "dev": st.st_dev,
                            "ino": st.st_ino,
                            "album": album_name,
                            "done": False,
                        }
                    )

            raw_entries.sort(
                key=lambda e: (
                    -int(e["size"]),
                    (int(e["dev"]), int(e["ino"])),
                    e.get("album") or "",
                    Path(e["path"]).name,
                )
            )
            index_entries = raw_entries
            _save_index(index_entries)
            self._log(
                f"[Sync] Saved video index '{index_path}' ({len(index_entries)} video(s), sorted largest first)."
            )

        total_candidates = len(index_entries)
        pending_entries = [e for e in index_entries if not e.get("done")]
        if pending_entries:
            max_mb = int(pending_entries[0]["size"]) / BYTES_PER_MB
            min_mb = int(pending_entries[-1]["size"]) / BYTES_PER_MB
            self._log(
                f"[Sync] Processing {len(pending_entries)}/{total_candidates} remaining video(s) "
                f"largest first ({max_mb:.1f} MB -> {min_mb:.1f} MB)..."
            )
        else:
            self._log(f"[Sync] All {total_candidates} indexed video(s) are already processed.")

        for idx, entry in enumerate(index_entries, start=1):
            fpath = Path(entry["path"])
            resolved = Path(entry["resolved"])
            album_name = entry.get("album")
            size_bytes = int(entry["size"])
            ino_key = (int(entry["dev"]), int(entry["ino"]))

            if entry.get("done"):
                result.skipped.append(
                    PhotoItem(
                        id=str(entry.get("youtube_id") or fpath),
                        filename=fpath.name,
                        platform=target.platform_name,
                        size=size_bytes,
                        album=album_name,
                        albums=[album_name] if album_name else [],
                    )
                )
                continue

            if not resolved.exists():
                self._log(f"[{idx}/{total_candidates}] Missing file (skipping): {fpath}")
                continue

            label = album_name or "_library"
            size_mb = size_bytes / BYTES_PER_MB
            self._log(
                f"[{idx}/{total_candidates}] {fpath.name} ({size_mb:.1f} MB, album={label})"
            )
            before_yt = len(result.routed_to_youtube)
            outcome = self._route_local_video_to_youtube(
                fpath=fpath,
                resolved=resolved,
                size_bytes=size_bytes,
                ino_key=ino_key,
                album_name=album_name,
                source=source,
                yt_target=target,
                inode_to_yt=inode_to_yt,
                result=result,
            )
            if outcome == "uploaded" and len(result.routed_to_youtube) > before_yt:
                uploaded_item = result.routed_to_youtube[-1]
                result.copied.append(uploaded_item)
                if not self.dry_run:
                    entry["done"] = True
                    entry["youtube_id"] = uploaded_item.id
                    _save_index(index_entries)
            elif outcome in {"linked", "skipped"}:
                if not self.dry_run:
                    entry["done"] = True
                    if ino_key in inode_to_yt:
                        entry["youtube_id"] = inode_to_yt[ino_key][0].id
                    _save_index(index_entries)
            elif outcome == "rate_limited":
                if not self.dry_run:
                    _save_index(index_entries)
                self._log(
                    "[YouTube] Upload/rate limit reached (429 / uploadLimitExceeded / quota). "
                    "Stopping YouTube export immediately; re-run later to resume from index."
                )
                break

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
