#!/usr/bin/env python3
"""Migrate and reorganize an Ente export directory into the crosspix layout.

Streams sequentially through flat Ente album directories (e.g. `<source>/<Album Name>/<photo>.jpg`)
without building a global catalog in memory, moves photos into canonical
`_library/YYYY/MM/<photo>.jpg` storage, deduplicates photos present in multiple Ente
albums locally against `_library/YYYY/MM/` (keeping a single physical inode on disk),
creates hierarchical hardlinks (or symlinks) under `albums/<word1>/<word2>/<photo>.jpg`
using space (`" "`) as the folder hierarchy delimiter, moves any remaining non-media
files and auxiliary folders (such as `.meta`) into the corresponding album directory,
cleans up empty legacy folders as it goes, and displays live progress in the terminal.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from crosspix.local import LinkMode, LocalProvider, SUPPORTED_EXTENSIONS

DEFAULT_SOURCE_DIR = "/home/goulu/moulinette/Images/Ente"
SYSTEM_FOLDERS = {"uncategorized", "trash", "corbeille", "archive", "hidden"}
TRASH_FOLDERS = {"trash", "corbeille"}


def _find_sidecars(file_path: Path) -> list[Path]:
    """Return any Ente or Google Takeout JSON sidecar files associated with file_path."""
    candidates = [
        file_path.parent / "metadata" / f"{file_path.name}.json",
        file_path.with_name(f"{file_path.name}.json"),
        file_path.with_name(f"{file_path.name}.supplemental-metadata.json"),
    ]
    found: list[Path] = []
    for c in candidates:
        try:
            if c.is_file():
                found.append(c)
        except OSError:
            pass
    return found


def _same_file_content(
    src_path: Path,
    existing_path: Path,
    src_sha256: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Return (is_identical, computed_src_sha256) using fast inode and size checks first."""
    st_src = src_path.stat()
    st_dst = existing_path.stat()
    if st_src.st_dev == st_dst.st_dev and st_src.st_ino == st_dst.st_ino:
        return True, src_sha256
    if st_src.st_size != st_dst.st_size:
        return False, src_sha256
    if src_sha256 is None:
        src_sha256 = LocalProvider.compute_sha256(src_path)
    dst_sha256 = LocalProvider.compute_sha256(existing_path)
    return src_sha256 == dst_sha256, src_sha256


def _resolve_canonical_target(
    file_path: Path,
    canonical_dir: Path,
    dry_run: bool,
    dry_run_targets: Optional[dict[Path, Path]] = None,
) -> tuple[Path, bool]:
    """Resolve the canonical `_library/YYYY/MM/` path for `file_path` without a global catalog.

    Checks only the target `canonical_dir` directly on disk (avoiding SHA-256 hashing
    altogether when no file already exists at the target path).

    Returns:
        `(canonical_path, is_duplicate)`
    """
    candidate = canonical_dir / file_path.name
    existing_ref: Optional[Path] = None
    if candidate.exists():
        existing_ref = candidate
    elif dry_run and dry_run_targets is not None and candidate in dry_run_targets:
        existing_ref = dry_run_targets[candidate]

    if existing_ref is None:
        if dry_run and dry_run_targets is not None:
            dry_run_targets[candidate] = file_path
        return candidate, False

    is_same, src_sha256 = _same_file_content(file_path, existing_ref)
    if is_same:
        return candidate, True

    # Different photo with the same filename in the same YYYY/MM -> disambiguate with SHA-256 prefix
    if src_sha256 is None:
        src_sha256 = LocalProvider.compute_sha256(file_path)

    disambiguated = canonical_dir / f"{file_path.stem}_{src_sha256[:8]}{file_path.suffix}"
    existing_disambig: Optional[Path] = None
    if disambiguated.exists():
        existing_disambig = disambiguated
    elif dry_run and dry_run_targets is not None and disambiguated in dry_run_targets:
        existing_disambig = dry_run_targets[disambiguated]

    if existing_disambig is not None:
        is_same_disambig, _ = _same_file_content(
            file_path, existing_disambig, src_sha256=src_sha256
        )
        if is_same_disambig:
            return disambiguated, True
        # Extremely unlikely 8-char hash collision on distinct files -> use full SHA-256
        full_disambiguated = canonical_dir / f"{file_path.stem}_{src_sha256}{file_path.suffix}"
        if dry_run and dry_run_targets is not None:
            dry_run_targets[full_disambiguated] = file_path
        return full_disambiguated, False

    if dry_run and dry_run_targets is not None:
        dry_run_targets[disambiguated] = file_path
    return disambiguated, False


def _move_extra_file(
    src_file: Path,
    dst_dir: Path,
    dry_run: bool,
) -> tuple[Path, bool]:
    """Move a non-media file (or symlink) into `dst_dir`, deduplicating if already present.

    Returns:
        `(destination_path, was_duplicate)`
    """
    dst_file = dst_dir / src_file.name

    if src_file.is_symlink():
        if dst_file.is_symlink():
            if os.readlink(src_file) == os.readlink(dst_file):
                if not dry_run:
                    src_file.unlink()
                return dst_file, True
            idx = 1
            while dst_file.exists() or dst_file.is_symlink():
                dst_file = dst_dir / f"{src_file.stem}_{idx}{src_file.suffix}"
                idx += 1
        elif dst_file.exists():
            idx = 1
            while dst_file.exists() or dst_file.is_symlink():
                dst_file = dst_dir / f"{src_file.stem}_{idx}{src_file.suffix}"
                idx += 1

        if not dry_run:
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_file), str(dst_file))
        return dst_file, False

    if dst_file.exists() and not dst_file.is_symlink() and dst_file.is_file():
        is_same, src_sha = _same_file_content(src_file, dst_file)
        if is_same:
            if not dry_run and src_file.resolve() != dst_file.resolve():
                src_file.unlink()
            return dst_file, True
        if src_sha is None:
            src_sha = LocalProvider.compute_sha256(src_file)
        disambiguated = dst_dir / f"{src_file.stem}_{src_sha[:8]}{src_file.suffix}"
        if disambiguated.exists() and disambiguated.is_file():
            is_same2, _ = _same_file_content(src_file, disambiguated, src_sha256=src_sha)
            if is_same2:
                if not dry_run and src_file.resolve() != disambiguated.resolve():
                    src_file.unlink()
                return disambiguated, True
            disambiguated = dst_dir / f"{src_file.stem}_{src_sha}{src_file.suffix}"
        dst_file = disambiguated

    if not dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_file), str(dst_file))
    return dst_file, False


def _move_extra_directory(
    src_dir: Path,
    dst_dir: Path,
    dry_run: bool,
) -> tuple[int, int]:
    """Move or merge an auxiliary directory (e.g. `.meta`) into `dst_dir`.

    Returns:
        `(moved_count, deduplicated_count)`
    """
    if not dst_dir.exists() and not dst_dir.is_symlink():
        file_count = 0
        for _, _, fnames in os.walk(src_dir):
            file_count += len(fnames)
        if not dry_run:
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_dir), str(dst_dir))
        return max(1, file_count), 0

    moved = 0
    dedup = 0
    for walk_root, subdirs, fnames in os.walk(src_dir, topdown=True):
        walk_path = Path(walk_root)
        rel_sub = walk_path.relative_to(src_dir)
        target_sub = dst_dir / rel_sub
        if not dry_run:
            target_sub.mkdir(parents=True, exist_ok=True)
        for fname in sorted(fnames):
            _, was_dup = _move_extra_file(walk_path / fname, target_sub, dry_run=dry_run)
            if was_dup:
                dedup += 1
            else:
                moved += 1
        subdirs.sort()

    if not dry_run:
        # Remove emptied source directory tree bottom-up
        for walk_root, _, _ in os.walk(src_dir, topdown=False):
            p = Path(walk_root)
            try:
                if not any(p.iterdir()):
                    p.rmdir()
            except OSError:
                pass

    return moved, dedup


def _remove_empty_dir_and_ancestors(path: Path, stop_at: Path) -> int:
    """Remove `path` if empty, then walk up removing empty parents until `stop_at`."""
    removed = 0
    curr = path
    while curr != stop_at and curr.is_relative_to(stop_at):
        try:
            if curr.exists() and not any(curr.iterdir()):
                curr.rmdir()
                removed += 1
                curr = curr.parent
            else:
                break
        except OSError:
            break
    return removed


def _format_elapsed(seconds: float) -> str:
    total_secs = int(seconds)
    hours, rem = divmod(total_secs, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


class _ProgressReporter:
    """Lightweight terminal progress reporter requiring no upfront file total."""

    def __init__(self, verbose: bool = False, dry_run: bool = False) -> None:
        self.verbose = verbose or dry_run
        self.dry_run = dry_run
        self.is_tty = sys.stdout.isatty()
        self.start_time = time.monotonic()
        self._last_render = 0.0
        self._status_active = False

    def _format_status(self, stats: dict[str, int], current_label: str) -> str:
        elapsed = time.monotonic() - self.start_time
        rate = stats["scanned"] / elapsed if elapsed > 0 else 0.0
        parts = [
            f"[{_format_elapsed(elapsed)}]",
            f"{stats['scanned']} files",
            f"moved: {stats['moved_to_library']}",
            f"dedup: {stats['deduplicated_copies']}",
            f"links: {stats['album_links_created']}",
        ]
        if stats["extra_files_moved"]:
            parts.append(f"extra: {stats['extra_files_moved']}")
        if stats["trash_skipped"]:
            parts.append(f"trash: {stats['trash_skipped']}")
        if stats["removed_dirs"]:
            parts.append(f"dirs: {stats['removed_dirs']}")
        parts.append(f"{rate:.1f}/s")

        prefix = " | ".join(parts) + " | "
        if self.is_tty:
            cols = shutil.get_terminal_size((100, 24)).columns
            max_label = max(12, cols - len(prefix) - 1)
            if len(current_label) > max_label:
                current_label = "..." + current_label[-(max_label - 3) :]
        return prefix + current_label

    def log_line(self, message: str) -> None:
        """Print a permanent log line cleanly without corrupting the live status bar."""
        if self._status_active and self.is_tty:
            sys.stdout.write("\r\033[K")
            self._status_active = False
        print(message, flush=True)

    def update(self, stats: dict[str, int], current_label: str, force: bool = False) -> None:
        """Refresh progress display in the terminal."""
        now = time.monotonic()
        if self.is_tty:
            if not force and (now - self._last_render) < 0.05:
                return
            line = self._format_status(stats, current_label)
            sys.stdout.write(f"\r\033[K{line}")
            sys.stdout.flush()
            self._status_active = True
            self._last_render = now
        elif not self.verbose:
            # Non-TTY output (e.g. redirected log): print periodic progress snapshots
            if not force and stats["scanned"] > 1 and (now - self._last_render) < 1.0:
                return
            print(self._format_status(stats, current_label), flush=True)
            self._last_render = now

    def finish(self, stats: dict[str, int], interrupted: bool = False) -> None:
        """Finalize the status line and print the summary."""
        if self._status_active and self.is_tty:
            sys.stdout.write("\r\033[K")
            self._status_active = False
        elapsed = _format_elapsed(time.monotonic() - self.start_time)
        prefix = "[Migrate] Interrupted!" if interrupted else "[Migrate] Done!"
        print(
            f"{prefix} ({elapsed}) Scanned: {stats['scanned']}, "
            f"Moved to _library: {stats['moved_to_library']}, "
            f"Duplicates eliminated: {stats['deduplicated_copies']}, "
            f"Album links created: {stats['album_links_created']}, "
            f"Extra items moved: {stats['extra_files_moved']}, "
            f"Trash skipped: {stats['trash_skipped']}, "
            f"Empty directories cleaned: {stats['removed_dirs']}.",
            flush=True,
        )


def migrate_ente_directory(
    source_dir: str | Path = DEFAULT_SOURCE_DIR,
    target_dir: Optional[str | Path] = None,
    album_delimiter: str = " ",
    link_mode: LinkMode = "hardlink",
    dry_run: bool = False,
    clean_empty_dirs: bool = True,
    skip_trash: bool = True,
    verbose: bool = False,
) -> dict[str, int]:
    """Stream through an Ente export folder sequentially and move photos into crosspix layout."""
    src_root = Path(source_dir).expanduser().resolve()
    dst_root = Path(target_dir).expanduser().resolve() if target_dir else src_root

    if not src_root.exists():
        raise FileNotFoundError(f"Source directory does not exist: {src_root}")

    provider = LocalProvider(
        root_dir=dst_root,
        album_delimiter=album_delimiter,
        link_mode=link_mode,
        use_library_layout=True,
    )
    if not dry_run:
        provider.connect()

    stats = {
        "scanned": 0,
        "moved_to_library": 0,
        "deduplicated_copies": 0,
        "album_links_created": 0,
        "extra_files_moved": 0,
        "trash_skipped": 0,
        "removed_dirs": 0,
    }

    # Only used during --dry-run (since nothing is actually written to `_library` on disk)
    dry_run_targets: Optional[dict[Path, Path]] = {} if dry_run else None
    progress = _ProgressReporter(verbose=verbose, dry_run=dry_run)
    progress.log_line(
        f"[Migrate] Processing {src_root} -> {dst_root}"
        + (" (DRY-RUN)" if dry_run else "")
    )

    interrupted = False
    try:
        for root_str, dirnames, filenames in os.walk(src_root, topdown=True):
            current_dir = Path(root_str)
            rel_dir = current_dir.relative_to(src_root)

            # Extract album name once for this directory
            raw_album_parts = [
                p.strip() for p in rel_dir.parts if p.strip() and p != "."
            ]
            raw_album_name = (
                album_delimiter.join(raw_album_parts) if raw_album_parts else None
            )
            is_trash_dir = bool(
                skip_trash
                and raw_album_parts
                and raw_album_parts[0].lower() in TRASH_FOLDERS
            )
            album_name = (
                None
                if (
                    not raw_album_name
                    or raw_album_name.lower() in SYSTEM_FOLDERS
                    or is_trash_dir
                )
                else raw_album_name
            )
            album_dir = (
                provider.albums_root / provider.album_to_relpath(album_name)
                if album_name
                else None
            )

            # Prune `_library/`, `albums/`, `metadata/`, target root, and hidden subdirs (e.g. `.meta`)
            pruned_dirs: list[str] = []
            hidden_dirs: list[str] = []
            for d in sorted(dirnames):
                if d == "metadata":
                    continue
                if rel_dir == Path(".") and d in {
                    provider.library_dirname,
                    provider.albums_dirname,
                }:
                    continue
                if dst_root != src_root and (current_dir / d).resolve() == dst_root:
                    continue
                if d.startswith("."):
                    hidden_dirs.append(d)
                    continue
                pruned_dirs.append(d)
            dirnames[:] = pruned_dirs

            folder_label = str(rel_dir) if rel_dir != Path(".") else "."
            progress.update(stats, f"dir: {folder_label}", force=False)

            sorted_filenames = sorted(filenames)
            media_filenames: list[str] = []
            extra_filenames: list[str] = []
            for filename in sorted_filenames:
                file_path = current_dir / filename
                if (
                    not file_path.is_symlink()
                    and file_path.is_file()
                    and file_path.suffix.lower() in SUPPORTED_EXTENSIONS
                ):
                    media_filenames.append(filename)
                else:
                    extra_filenames.append(filename)

            processed_sidecars: set[Path] = set()

            # Pass 1: process media files in this directory
            for filename in media_filenames:
                file_path = current_dir / filename
                stats["scanned"] += 1
                rel_path = file_path.relative_to(src_root)

                if is_trash_dir:
                    stats["trash_skipped"] += 1
                    if progress.verbose:
                        progress.log_line(
                            f"[{stats['scanned']}] Skipping trashed file: {rel_path}"
                        )
                    progress.update(stats, str(rel_path))
                    continue

                # Read capture timestamp BEFORE moving (so sidecar metadata/<file>.json is found)
                _, _, created_at, _ = LocalProvider.extract_image_metadata(file_path)
                sidecars = _find_sidecars(file_path)

                dt = created_at or datetime.fromtimestamp(file_path.stat().st_mtime)
                canonical_dir = (
                    provider.library_root / f"{dt.year:04d}" / f"{dt.month:02d}"
                )
                canonical_path, is_duplicate = _resolve_canonical_target(
                    file_path=file_path,
                    canonical_dir=canonical_dir,
                    dry_run=dry_run,
                    dry_run_targets=dry_run_targets,
                )

                if not is_duplicate:
                    if dry_run:
                        stats["moved_to_library"] += 1
                        progress.log_line(
                            f"[{stats['scanned']}] [DRY-RUN] Move: {rel_path} -> "
                            f"{canonical_path.relative_to(dst_root)}"
                        )
                    else:
                        canonical_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(file_path), str(canonical_path))
                        if created_at:
                            ts = created_at.timestamp()
                            os.utime(canonical_path, (ts, ts))
                        stats["moved_to_library"] += 1
                        if progress.verbose:
                            progress.log_line(
                                f"[{stats['scanned']}] Moved to library: {rel_path} -> "
                                f"{canonical_path.relative_to(dst_root)}"
                            )
                else:
                    stats["deduplicated_copies"] += 1
                    if dry_run:
                        progress.log_line(
                            f"[{stats['scanned']}] [DRY-RUN] Remove duplicate: {rel_path} "
                            f"(matches {canonical_path.relative_to(dst_root)})"
                        )
                    else:
                        if file_path.resolve() != canonical_path.resolve():
                            file_path.unlink()
                        if progress.verbose:
                            progress.log_line(
                                f"[{stats['scanned']}] Removed duplicate copy: {rel_path} "
                                f"(matches {canonical_path.relative_to(dst_root)})"
                            )

                # Create hierarchical hardlink/symlink in `albums/<word1>/<word2>/`
                if album_name and album_dir is not None:
                    link_path = album_dir / canonical_path.name
                    if dry_run:
                        stats["album_links_created"] += 1
                        progress.log_line(
                            f"[{stats['scanned']}] [DRY-RUN] Link ({link_mode}): "
                            f"{link_path.relative_to(dst_root)} -> "
                            f"{canonical_path.relative_to(dst_root)}"
                        )
                    else:
                        created_link = provider._create_album_link(
                            canonical_path, album_name
                        )
                        stats["album_links_created"] += 1
                        if progress.verbose:
                            progress.log_line(
                                f"[{stats['scanned']}] Linked ({link_mode}): "
                                f"{created_link.relative_to(dst_root)} -> "
                                f"{canonical_path.relative_to(dst_root)}"
                            )

                # Remove processed sidecar JSON files so legacy folders can be cleaned immediately
                if clean_empty_dirs:
                    for sc in sidecars:
                        processed_sidecars.add(sc)
                        if not dry_run:
                            try:
                                sc.unlink()
                            except OSError:
                                pass

                progress.update(stats, str(rel_path))

            # Pass 2: move remaining non-media files and auxiliary dirs (e.g. `.meta`) into the album folder
            if album_dir is not None:
                for filename in extra_filenames:
                    file_path = current_dir / filename
                    if file_path in processed_sidecars:
                        continue
                    if not (file_path.exists() or file_path.is_symlink()):
                        continue

                    rel_path = file_path.relative_to(src_root)
                    dst_path, was_dup = _move_extra_file(
                        file_path, album_dir, dry_run=dry_run
                    )
                    if was_dup:
                        stats["deduplicated_copies"] += 1
                        if progress.verbose:
                            tag = "[DRY-RUN] Remove duplicate extra" if dry_run else "Removed duplicate extra"
                            progress.log_line(
                                f"[extra] {tag}: {rel_path} "
                                f"(matches {dst_path.relative_to(dst_root)})"
                            )
                    else:
                        stats["extra_files_moved"] += 1
                        if progress.verbose:
                            tag = "[DRY-RUN] Move extra" if dry_run else "Moved extra"
                            progress.log_line(
                                f"[extra] {tag}: {rel_path} -> "
                                f"{dst_path.relative_to(dst_root)}"
                            )
                    progress.update(stats, str(rel_path))

                for hidden_name in hidden_dirs:
                    src_hidden = current_dir / hidden_name
                    if not src_hidden.is_dir():
                        continue
                    dst_hidden = album_dir / hidden_name
                    rel_hidden = src_hidden.relative_to(src_root)
                    moved_cnt, dedup_cnt = _move_extra_directory(
                        src_hidden, dst_hidden, dry_run=dry_run
                    )
                    stats["extra_files_moved"] += moved_cnt
                    stats["deduplicated_copies"] += dedup_cnt
                    if progress.verbose:
                        tag = "[DRY-RUN] Move extra dir" if dry_run else "Moved extra dir"
                        progress.log_line(
                            f"[extra] {tag}: {rel_hidden} -> "
                            f"{dst_hidden.relative_to(dst_root)}"
                        )
                    progress.update(stats, str(rel_hidden))

            # Clean up `metadata/` (delete if empty, or move leftover unconsumed files to album)
            meta_dir = current_dir / "metadata"
            if meta_dir.is_dir() and not is_trash_dir:
                if clean_empty_dirs and not dry_run:
                    try:
                        if not any(meta_dir.iterdir()):
                            meta_dir.rmdir()
                            stats["removed_dirs"] += 1
                    except OSError:
                        pass
                if (
                    clean_empty_dirs
                    and album_dir is not None
                    and meta_dir.is_dir()
                    and any(
                        p not in processed_sidecars
                        for p in meta_dir.rglob("*")
                        if p.is_file() or p.is_symlink()
                    )
                ):
                    dst_meta = album_dir / "metadata"
                    rel_meta = meta_dir.relative_to(src_root)
                    moved_cnt, dedup_cnt = _move_extra_directory(
                        meta_dir, dst_meta, dry_run=dry_run
                    )
                    stats["extra_files_moved"] += moved_cnt
                    stats["deduplicated_copies"] += dedup_cnt
                    if progress.verbose:
                        tag = "[DRY-RUN] Move leftover metadata" if dry_run else "Moved leftover metadata"
                        progress.log_line(
                            f"[extra] {tag}: {rel_meta} -> "
                            f"{dst_meta.relative_to(dst_root)}"
                        )
                    progress.update(stats, str(rel_meta))

            # Immediately clean up empty legacy folder once processed
            if clean_empty_dirs and not dry_run and not is_trash_dir:
                if current_dir != src_root and not dirnames:
                    stats["removed_dirs"] += _remove_empty_dir_and_ancestors(
                        current_dir, src_root
                    )
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        progress.finish(stats, interrupted=interrupted)

    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sequentially reorganize an Ente export directory into crosspix's "
            "_library/YYYY/MM/ and hierarchical albums/ layout using hardlinks."
        )
    )
    parser.add_argument(
        "source",
        nargs="?",
        default=DEFAULT_SOURCE_DIR,
        help=f"Source Ente export directory (default: {DEFAULT_SOURCE_DIR}).",
    )
    parser.add_argument(
        "--target",
        "-t",
        help=(
            "Optional target root directory (e.g. /home/goulu/Images/Photos). "
            "Defaults to reorganizing the source directory in-place."
        ),
    )
    parser.add_argument(
        "--album-delimiter",
        default=" ",
        help="Delimiter to split album names into folder hierarchies (default: ' ').",
    )
    parser.add_argument(
        "--link-mode",
        choices=["hardlink", "symlink", "copy"],
        default="hardlink",
        help="Link mode for album folders (default: hardlink).",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview all file moves and hardlinks without modifying the disk.",
    )
    parser.add_argument(
        "--keep-empty-dirs",
        action="store_true",
        help="Do not delete sidecar JSONs or empty source album directories after moving photos.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print each individual file move, deduplication, and link action.",
    )

    args = parser.parse_args(argv)
    try:
        migrate_ente_directory(
            source_dir=args.source,
            target_dir=args.target,
            album_delimiter=args.album_delimiter,
            link_mode=args.link_mode,
            dry_run=args.dry_run,
            clean_empty_dirs=not args.keep_empty_dirs,
            verbose=args.verbose,
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
