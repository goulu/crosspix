#!/usr/bin/env python3
"""Adjust local file modification times and matching Immich asset dates from filenames.

Walks a directory (e.g. `/home/goulu/Images/Ente/_library`) sequentially, extracts dates
formatted as `YYYYMMDD` (or `YYYY-MM-DD`) with an optional `HHMMSS` time (e.g.
`PXL_20260821_082337164.mp4`, `20150702_201509.jpg`, `IMG-20160221-WA0012.jpeg`) from
each media filename — defaulting to `12:00:00` when no time is present — and:
  1. Updates the local file's `mtime` via `os.utime` if it differs from the filename date.
  2. Looks up the corresponding asset in Immich via batched `POST /api/assets/bulk-upload-check`
     (SHA-1) and updates its `dateTimeOriginal` via `PUT /api/assets/{id}` if it exists
     and differs.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from crosspix.cli import _read_key_file
from crosspix.immich import ImmichProvider
from crosspix.local import LocalProvider, SUPPORTED_EXTENSIONS

DEFAULT_ROOT_DIR = "/home/goulu/Images/Ente/_library"
DEFAULT_IMMICH_URL = "http://localhost:2283/api"

# Matches YYYYMMDD or YYYY-MM-DD / YYYY_MM_DD surrounded by non-digit boundaries,
# optionally followed by separator + HHMMSS (with optional 3 millisecond digits like PXL_..._082337164)
# or HH-MM-SS / HH_MM_SS / HH.MM.SS.
_FILENAME_DATE_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>(?:19|20)\d{2})(?P<dsep>[-_]?)"
    r"(?P<month>0[1-9]|1[0-2])(?P=dsep)"
    r"(?P<day>0[1-9]|[12]\d|3[01])"
    r"(?!\d)"
    r"(?:"
    r"[\s_\-T]"
    r"(?:"
    r"(?P<hour>[01]\d|2[0-3])(?P<minute>[0-5]\d)(?P<second>[0-5]\d)(?:\d{3})?(?!\d)"
    r"|"
    r"(?P<hour2>[01]\d|2[0-3])(?P<tsep>[-_.])(?P<minute2>[0-5]\d)(?P=tsep)(?P<second2>[0-5]\d)(?!\d)"
    r")"
    r")?",
    re.IGNORECASE,
)


def extract_datetime_from_filename(filename: str) -> Optional[datetime]:
    """Parse a `YYYYMMDD` (+ optional `HHMMSS`) timestamp from a filename.

    When the filename contains a date without a time (e.g. `IMG-20160221-WA0012.jpeg`),
    defaults the time to `12:00:00`. Returns a naive local `datetime`, or `None` if no
    valid date is found.
    """
    stem = Path(filename).stem
    for match in _FILENAME_DATE_RE.finditer(stem):
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        if match.group("hour") is not None:
            hour = int(match.group("hour"))
            minute = int(match.group("minute"))
            second = int(match.group("second"))
        elif match.group("hour2") is not None:
            hour = int(match.group("hour2"))
            minute = int(match.group("minute2"))
            second = int(match.group("second2"))
        else:
            hour, minute, second = 12, 0, 0

        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            continue
    return None


def _parse_immich_datetime(asset_info: dict) -> Optional[datetime]:
    """Extract an Immich asset's effective capture timestamp as an aware UTC datetime."""
    exif_info = asset_info.get("exifInfo") or {}
    raw = (
        exif_info.get("dateTimeOriginal")
        or asset_info.get("fileCreatedAt")
        or asset_info.get("localDateTime")
    )
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class _Progress:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.is_tty = sys.stdout.isatty()
        self.start = time.monotonic()
        self._last = 0.0
        self._active = False

    def log(self, message: str) -> None:
        if self._active and self.is_tty:
            sys.stdout.write("\r\033[K")
            self._active = False
        print(message, flush=True)

    def update(self, message: str, force: bool = False) -> None:
        now = time.monotonic()
        if self.is_tty:
            if not force and (now - self._last) < 0.05:
                return
            cols = shutil.get_terminal_size((100, 24)).columns
            line = f"[{_format_elapsed(now - self.start)}] {message}"
            if len(line) >= cols:
                line = line[: cols - 4] + "..."
            sys.stdout.write(f"\r\033[K{line}")
            sys.stdout.flush()
            self._active = True
            self._last = now
        elif not self.verbose:
            if not force and (now - self._last) < 2.0:
                return
            print(f"[{_format_elapsed(now - self.start)}] {message}", flush=True)
            self._last = now

    def clear(self) -> None:
        if self._active and self.is_tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self._active = False


def _non_negative_int(value: str) -> int:
    try:
        ival = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid integer value: {value!r} (expected a whole number of seconds)"
        ) from exc
    if ival < 0:
        raise argparse.ArgumentTypeError("tolerance must be >= 0 seconds")
    return ival


def fix_dates_from_filenames(
    root_dir: str | Path = DEFAULT_ROOT_DIR,
    immich: Optional[ImmichProvider] = None,
    local_only: bool = False,
    immich_only: bool = False,
    dry_run: bool = False,
    tolerance_seconds: int = 2,
    batch_size: int = 50,
    error_log_path: Optional[str | Path] = None,
    verbose: bool = False,
) -> dict[str, int]:
    """Scan `root_dir`, adjust local `mtime` and matching Immich `dateTimeOriginal` from filenames."""
    root = Path(root_dir).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Directory does not exist: {root}")

    use_immich = immich is not None and not local_only
    if use_immich and immich is not None:
        immich.connect()

    err_log = Path(error_log_path).expanduser() if error_log_path else None
    progress = _Progress(verbose=verbose)
    t0 = time.monotonic()

    stats = {
        "scanned": 0,
        "dated_files": 0,
        "local_updated": 0,
        "local_already_ok": 0,
        "immich_matched": 0,
        "immich_updated": 0,
        "immich_already_ok": 0,
        "immich_not_found": 0,
        "errors": 0,
    }

    def record_error(fpath: Path, context: str, exc: Exception) -> None:
        stats["errors"] += 1
        progress.log(f"  Error ({context}) '{fpath.name}': {exc}")
        if err_log is not None:
            try:
                err_log.parent.mkdir(parents=True, exist_ok=True)
                ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with err_log.open("a", encoding="utf-8") as fh:
                    fh.write(f"[{ts_str}] fix_dates | {context} | {fpath} | {exc}\n")
            except OSError:
                pass

    mode_tag = " (DRY-RUN)" if dry_run else ""
    target_desc = (
        "local only"
        if not use_immich
        else ("Immich only" if immich_only else f"local + Immich ({immich.base_url})")
    )
    progress.log(f"[FixDates] Scanning {root} [{target_desc}]{mode_tag}...")

    def flush_immich_batch(batch: list[tuple[Path, datetime, str]]) -> None:
        """Check a batch of `(fpath, target_dt, sha1_hex)` against Immich and update mismatched dates."""
        if not batch or not use_immich or immich is None:
            return

        check_items = [(str(idx), sha1) for idx, (_, _, sha1) in enumerate(batch)]
        try:
            check_results = immich.check_bulk_upload(check_items)
        except Exception as exc:
            for fpath, _, _ in batch:
                record_error(fpath, "immich bulk-upload-check", exc)
            return

        for idx, (fpath, target_dt, _) in enumerate(batch):
            action, asset_id = check_results.get(str(idx), ("accept", None))
            if action != "reject" or not asset_id:
                stats["immich_not_found"] += 1
                continue

            stats["immich_matched"] += 1
            aware_local = target_dt.astimezone()
            target_utc = aware_local.astimezone(timezone.utc)
            target_iso = aware_local.isoformat()

            try:
                asset_info = immich.get_asset_info(asset_id)
                current_utc = _parse_immich_datetime(asset_info)
                if (
                    current_utc is not None
                    and abs((current_utc - target_utc).total_seconds()) <= tolerance_seconds
                ):
                    stats["immich_already_ok"] += 1
                    continue

                if dry_run:
                    stats["immich_updated"] += 1
                    if verbose:
                        cur_str = current_utc.isoformat() if current_utc else "unknown"
                        progress.log(
                            f"  [DRY-RUN] [Immich] {fpath.name} (id={asset_id}): {cur_str} -> {target_iso}"
                        )
                else:
                    immich.update_asset_date(asset_id, target_iso)
                    stats["immich_updated"] += 1
                    if verbose:
                        cur_str = current_utc.isoformat() if current_utc else "unknown"
                        progress.log(
                            f"  [Immich] Updated {fpath.name} (id={asset_id}): {cur_str} -> {target_iso}"
                        )
            except Exception as exc:
                record_error(fpath, f"immich update id={asset_id}", exc)

    immich_batch: list[tuple[Path, datetime, str]] = []

    for walk_root, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d != "metadata"
        )
        rp = Path(walk_root)
        rel_dir = str(rp.relative_to(root)) if rp != root else "."

        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            fpath = rp / fname
            if fpath.is_symlink() or not fpath.is_file():
                continue

            stats["scanned"] += 1
            target_dt = extract_datetime_from_filename(fname)
            if target_dt is None:
                continue

            stats["dated_files"] += 1
            target_ts = target_dt.timestamp()

            # 1. Adjust local file mtime
            if not immich_only:
                try:
                    st = fpath.stat()
                    if abs(st.st_mtime - target_ts) > tolerance_seconds:
                        if dry_run:
                            stats["local_updated"] += 1
                            if verbose:
                                old_dt = datetime.fromtimestamp(st.st_mtime).isoformat(
                                    timespec="seconds"
                                )
                                progress.log(
                                    f"  [DRY-RUN] [Local] {fpath.relative_to(root)}: "
                                    f"{old_dt} -> {target_dt.isoformat(timespec='seconds')}"
                                )
                        else:
                            os.utime(fpath, (target_ts, target_ts))
                            stats["local_updated"] += 1
                            if verbose:
                                old_dt = datetime.fromtimestamp(st.st_mtime).isoformat(
                                    timespec="seconds"
                                )
                                progress.log(
                                    f"  [Local] Updated {fpath.relative_to(root)}: "
                                    f"{old_dt} -> {target_dt.isoformat(timespec='seconds')}"
                                )
                    else:
                        stats["local_already_ok"] += 1
                except Exception as exc:
                    record_error(fpath, "local utime", exc)

            # 2. Queue for Immich date update
            if use_immich:
                try:
                    sha1_hex = LocalProvider.compute_sha1_hex(fpath)
                    immich_batch.append((fpath, target_dt, sha1_hex))
                    if len(immich_batch) >= batch_size:
                        flush_immich_batch(immich_batch)
                        immich_batch.clear()
                except Exception as exc:
                    record_error(fpath, "sha1 hash", exc)

            progress.update(
                f"{rel_dir} | dated: {stats['dated_files']}/{stats['scanned']} | "
                f"local: {stats['local_updated']} | immich: {stats['immich_updated']} | "
                f"err: {stats['errors']}"
            )

    if immich_batch:
        flush_immich_batch(immich_batch)
        immich_batch.clear()

    progress.clear()
    elapsed = _format_elapsed(time.monotonic() - t0)
    progress.log(
        f"[FixDates] Done{mode_tag}! ({elapsed})\n"
        f"  Media files scanned     : {stats['scanned']} (with filename date: {stats['dated_files']})\n"
        f"  Local mtime updated     : {stats['local_updated']} (already ok: {stats['local_already_ok']})\n"
        f"  Immich assets matched   : {stats['immich_matched']} (not in Immich: {stats['immich_not_found']})\n"
        f"  Immich dates updated    : {stats['immich_updated']} (already ok: {stats['immich_already_ok']})\n"
        f"  Errors                  : {stats['errors']}"
    )
    if stats["errors"] and err_log is not None:
        progress.log(f"  Errors logged to        : {err_log}")
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Explore a folder, parse YYYYMMDD (+ optional HHMMSS, default 12:00:00) from "
            "filenames, adjust local modification timestamps, and update matching assets in Immich."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=DEFAULT_ROOT_DIR,
        help=f"Directory to scan (default: {DEFAULT_ROOT_DIR}).",
    )
    parser.add_argument(
        "--immich-url",
        default=os.environ.get("IMMICH_URL", DEFAULT_IMMICH_URL),
        help=f"Immich server URL (default: {DEFAULT_IMMICH_URL}).",
    )
    parser.add_argument(
        "--immich-key",
        help="Immich API key (defaults to IMMICH_API_KEY env var or --immich-key-file).",
    )
    parser.add_argument(
        "--immich-key-file",
        default="immich.token",
        help="Path to gitignored Immich API key file (default: immich.token).",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Update local file modification times only without contacting Immich.",
    )
    parser.add_argument(
        "--immich-only",
        action="store_true",
        help="Update Immich asset dates only without modifying local file timestamps.",
    )
    parser.add_argument(
        "--tolerance",
        type=_non_negative_int,
        default=2,
        help="Skip updates when existing timestamp is already within this many whole seconds (default: 2).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of files checked per Immich bulk-upload-check call (default: 50).",
    )
    parser.add_argument(
        "--error-log",
        default="error.log",
        help="File where per-file errors are logged (default: error.log).",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview all local and Immich date changes without modifying anything.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print every updated file and asset.",
    )

    args = parser.parse_args(argv)
    try:
        immich_provider: Optional[ImmichProvider] = None
        if not args.local_only:
            api_key = (
                args.immich_key
                or os.environ.get("IMMICH_API_KEY", "")
                or _read_key_file(args.immich_key_file)
            )
            if api_key:
                immich_provider = ImmichProvider(base_url=args.immich_url, api_key=api_key)
            elif args.immich_only:
                raise ValueError(
                    "No Immich API key found (provide --immich-key, IMMICH_API_KEY, or immich.token)."
                )
            else:
                print(
                    "[FixDates] Warning: no Immich API key found; running in local-only mode.",
                    flush=True,
                )

        fix_dates_from_filenames(
            root_dir=args.root,
            immich=immich_provider,
            local_only=args.local_only,
            immich_only=args.immich_only,
            dry_run=args.dry_run,
            tolerance_seconds=args.tolerance,
            batch_size=args.batch_size,
            error_log_path=args.error_log,
            verbose=args.verbose,
        )
        return 0
    except KeyboardInterrupt:
        print("\n[FixDates] Interrupted.", flush=True)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
