#!/usr/bin/env python3
"""Deduplicate image variants in `_library`, then repair `albums/` links sequentially.

Supports duplicate variants created by Flickr exports, Google Photos edits, Takeout copies,
and crosspix disambiguation suffixes within each `_library/YYYY/MM/` folder:
  - Flickr originals: `<base>_<id>_o.<ext>` (and `<base>_<id>_o_<N>.<ext>`)
  - Edited versions: `<base>-modifié.<ext>`, `<base>-modifié(1).<ext>`, `<base>-edited.<ext>`
  - Hash-disambiguated duplicates: `<base>_<8hex>.<ext>` (e.g. `IMG-20160221-WA0012_403659de.jpg`)
  - Numbered copy duplicates: `<base>(1).<ext>`, `<base> (1).<ext>` (e.g. `IMG-20160221-WA0019(1).jpg`)

Works in two fast sequential phases with live terminal progress (no slow upfront indexing):
  Phase 1 (`_library/YYYY/MM/`):
    Groups candidate variants purely by filename strings (zero `stat()` calls on unique files).
    - Keeps `_<id>_o` first, then `-modifié`, then highest resolution & largest file size.
    - For `_<8hex>`, `(N)`, and multi-`_o` groups, clusters images visually using perceptual
      64-bit dHash (plus grayscale similarity for low-contrast images) so distinct photos that
      merely share a filename or Flickr title are NEVER merged or deleted.
    - Renames the kept file to the short base filename (e.g. `20140726_104622.jpg` or
      `IMG-20160221-WA0012.jpeg`) and deletes redundant copies.

  Phase 2 (`albums/`):
    Walks each album directory one after another via `os.scandir` (skipping non-media files
    and hidden folders before calling `stat()`), updating/rebuilding hardlinks and symlinks
    affected by Phase 1 as well as any broken symlinks or orphaned (`nlink == 1`) hardlinks.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
import warnings
from typing import Optional

from PIL import Image, ImageOps

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from crosspix.local import SUPPORTED_EXTENSIONS

warnings.filterwarnings("ignore", category=UserWarning, module="PIL.TiffImagePlugin")

DEFAULT_ROOT_DIR = "/home/goulu/Images/Ente"

IMAGE_EXTS = {
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

FLICKR_O_RE = re.compile(
    r"^(?P<base>.+?)_(?P<id>\d+)_o(?:_(?:\d+|[0-9a-f]{8}))?$",
    re.IGNORECASE,
)
MODIFIE_RE = re.compile(
    r"^(?P<base>.+?)(?:[-_.]modifi[eé]|-edited)(?:\(\d+\))?$",
    re.IGNORECASE,
)
HEX8_RE = re.compile(r"^(?P<base>.+?)_(?P<hex>[0-9a-f]{8})$", re.IGNORECASE)
DATE_8DIGIT_RE = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")
PAREN_COPY_RE = re.compile(r"^(?P<base>.+?)\s*\(\d+\)$")

VARIANT_KINDS = {"original_o", "modified", "disambig", "copy"}


@dataclass
class VariantInfo:
    path: Path
    base_stem: str
    ext_key: str
    kind: str  # "original_o", "modified", "disambig", "copy", "plain"
    flickr_id: Optional[str] = None
    size: int = 0
    pixels: int = 0


def _normalize_ext(ext: str) -> str:
    low = ext.lower()
    if low == ".jpeg":
        return ".jpg"
    return low


def parse_variant(path: Path, size: int = 0) -> VariantInfo:
    """Classify a filename as `original_o`, `modified`, `disambig`, `copy`, or `plain` without touching disk."""
    ext = path.suffix
    stem = path.stem

    if ext.lower() in {".modifié", ".modifie"}:
        m_o = FLICKR_O_RE.match(stem)
        if m_o:
            return VariantInfo(
                path=path,
                base_stem=m_o.group("base"),
                ext_key=".jpg",
                kind="modified",
                flickr_id=m_o.group("id"),
                size=size,
            )
        return VariantInfo(
            path=path,
            base_stem=stem,
            ext_key=".jpg",
            kind="modified",
            size=size,
        )

    ext_key = _normalize_ext(ext)
    work = stem
    kind = "plain"
    flickr_id: Optional[str] = None

    # Strip trailing crosspix `_<8hex>` disambiguation suffix (never strip YYYYMMDD dates!)
    m_hex = HEX8_RE.match(work)
    if m_hex and not DATE_8DIGIT_RE.match(m_hex.group("hex")):
        work = m_hex.group("base")
        kind = "disambig"

    # Strip trailing `(1)`, `(2)` copy suffix (except when attached directly to `-modifié(1)`)
    if not MODIFIE_RE.match(work):
        m_par = PAREN_COPY_RE.match(work)
        if m_par:
            work = m_par.group("base")
            if kind == "plain":
                kind = "copy"

    # Check `-modifié` / `-edited` suffix
    m_mod = MODIFIE_RE.match(work)
    if m_mod:
        inner = m_mod.group("base")
        m_inner_o = FLICKR_O_RE.match(inner)
        if m_inner_o:
            return VariantInfo(
                path=path,
                base_stem=m_inner_o.group("base"),
                ext_key=ext_key,
                kind="modified",
                flickr_id=m_inner_o.group("id"),
                size=size,
            )
        return VariantInfo(
            path=path,
            base_stem=inner,
            ext_key=ext_key,
            kind="modified",
            size=size,
        )

    # Check Flickr `_<id>_o` suffix
    m_o = FLICKR_O_RE.match(work)
    if m_o:
        return VariantInfo(
            path=path,
            base_stem=m_o.group("base"),
            ext_key=ext_key,
            kind="original_o",
            flickr_id=m_o.group("id"),
            size=size,
        )

    return VariantInfo(
        path=path,
        base_stem=work,
        ext_key=ext_key,
        kind=kind,
        flickr_id=flickr_id,
        size=size,
    )


@dataclass
class _VisualSig:
    dhash: Optional[int]
    dims: Optional[tuple[int, int]]  # true oriented (width, height)
    gray_pixels: Optional[list[int]]  # 9x8 grayscale for low-contrast fallback


def _compute_visual_sig(path: Path) -> _VisualSig:
    if path.suffix.lower() not in IMAGE_EXTS:
        return _VisualSig(None, None, None)
    try:
        with Image.open(path) as im:
            orig_w, orig_h = im.size
            try:
                orient = im.getexif().get(0x0112)
                if orient in (5, 6, 7, 8):
                    orig_w, orig_h = orig_h, orig_w
            except Exception:
                pass
            true_dims = (orig_w, orig_h)
            try:
                im.draft("L", (64, 64))
            except Exception:
                pass
            im_t = ImageOps.exif_transpose(im)
            gray = im_t.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
            pixels = list(
                gray.get_flattened_data()
                if hasattr(gray, "get_flattened_data")
                else gray.getdata()
            )
            bits = 0
            for row in range(8):
                offset = row * 9
                for col in range(8):
                    bits = (bits << 1) | (
                        1 if pixels[offset + col] > pixels[offset + col + 1] else 0
                    )
            return _VisualSig(bits, true_dims, pixels)
    except Exception:
        return _VisualSig(None, None, None)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_visual_sig(path: Path, cache: dict[Path, _VisualSig]) -> _VisualSig:
    sig = cache.get(path)
    if sig is None:
        sig = _compute_visual_sig(path)
        cache[path] = sig
    return sig


def _visually_same(
    p1: Path,
    p2: Path,
    cache: dict[Path, _VisualSig],
    max_hamming: int = 6,
    allow_low_contrast_mad: bool = True,
) -> bool:
    if p1.stat().st_size == p2.stat().st_size and _sha256_file(p1) == _sha256_file(p2):
        return True
    s1 = _get_visual_sig(p1, cache)
    s2 = _get_visual_sig(p2, cache)
    if s1.dhash is None or s2.dhash is None:
        return False
    if s1.dims and s2.dims and s1.dims[1] and s2.dims[1]:
        r1, r2 = s1.dims[0] / s1.dims[1], s2.dims[0] / s2.dims[1]
        if abs(r1 - r2) / max(r1, r2) > 0.08:
            return False

    dist = (s1.dhash ^ s2.dhash).bit_count()
    pop1 = s1.dhash.bit_count()
    if 8 <= pop1 <= 56:
        return dist <= max_hamming

    # Low-contrast / very dark or uniform image: verify pixel brightness closeness too
    if allow_low_contrast_mad and dist <= max_hamming and s1.gray_pixels and s2.gray_pixels:
        mad = sum(abs(a - b) for a, b in zip(s1.gray_pixels, s2.gray_pixels)) / len(s1.gray_pixels)
        return mad <= 6.0
    return False


def _cluster_group(items: list[VariantInfo]) -> list[tuple[list[VariantInfo], bool]]:
    """Cluster a candidate group safely so visually distinct photos are NEVER merged.

    Rules:
    - Simple `plain` + `-modifié` (and/or 1 `_o`, with NO `_<8hex>`/`(N)`): trusted as a single
      variant group (supports cropped/edited `-modifié` photos whose dHash may differ).
    - Any group containing `_<8hex>` / `(N)` OR `>=2` `_o` files: visually clustered via dHash!
      - If all files belong to 1 visual cluster -> `(items, allow_short_rename=True)`.
      - If there are multiple visually distinct photos sharing the same base name:
        - At most one cluster (the one owning a `plain` file, if any) gets `allow_short_rename=True`.
        - All other distinct clusters keep `allow_short_rename=False` (preserving unique names).
    """
    o_items = [it for it in items if it.kind == "original_o"]
    has_dup_suffix = any(it.kind in {"disambig", "copy"} for it in items)

    if len(o_items) < 2 and not has_dup_suffix:
        return [(items, True)]

    cache: dict[Path, _VisualSig] = {}

    # Populate true pixel counts for resolution-aware winner selection
    for it in items:
        sig = _get_visual_sig(it.path, cache)
        if sig.dims:
            it.pixels = sig.dims[0] * sig.dims[1]

    # Order seeds: `_o` first, then `plain`, then `modified`, then `disambig`/`copy` (largest first)
    seed_order = {"original_o": 0, "plain": 1, "modified": 2, "disambig": 3, "copy": 4}
    ordered = sorted(items, key=lambda it: (seed_order[it.kind], -it.pixels, -it.size, it.path.name))

    clusters: list[list[VariantInfo]] = []
    for it in ordered:
        placed = False
        for cl in clusters:
            # Strict threshold when merging two different `_o` Flickr IDs
            strict = it.kind == "original_o" and cl[0].kind == "original_o"
            if _visually_same(
                it.path,
                cl[0].path,
                cache,
                max_hamming=4 if strict else 6,
                allow_low_contrast_mad=not strict,
            ):
                cl.append(it)
                placed = True
                break
            # Allow `-modifié` attached to the same base when there's only 1 plain/o cluster?
            # Checked after loop below if needed.
        if not placed:
            clusters.append([it])

    if len(clusters) == 1:
        return [(clusters[0], True)]

    # Multiple visually distinct photos share this base name!
    # Only a cluster containing the plain short-named file may rename to the short name.
    result: list[tuple[list[VariantInfo], bool]] = []
    for cl in clusters:
        owns_plain = any(it.kind == "plain" for it in cl)
        result.append((cl, owns_plain))
    return result


def _choose_winner(items: list[VariantInfo]) -> VariantInfo:
    """Pick the winning variant: `_o` > `-modifié` > highest resolution & largest file size."""
    priority = {
        "original_o": 0,
        "modified": 1,
        "plain": 2,
        "disambig": 2,
        "copy": 2,
    }
    plain_tiebreak = lambda it: 0 if it.kind == "plain" else 1
    return min(
        items,
        key=lambda it: (priority[it.kind], -it.pixels, -it.size, plain_tiebreak(it), it.path.name),
    )


def _format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{num_bytes} B"


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class _Progress:
    """Live single-line progress bar on TTY, throttled periodic updates when non-TTY."""

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


def _iter_album_dirs(albums_root: Path):
    """Yield `(album_dir, media_dir_entries)` sequentially using fast `os.scandir`."""
    stack: list[Path] = [albums_root]
    while stack:
        current = stack.pop()
        subdirs: list[Path] = []
        media_entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(current) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith(".") or name == "metadata":
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        subdirs.append(Path(entry.path))
                    else:
                        ext = os.path.splitext(name)[1].lower()
                        if ext in SUPPORTED_EXTENSIONS or ext in {".modifié", ".modifie"}:
                            media_entries.append(entry)
        except OSError:
            continue

        if media_entries:
            media_entries.sort(key=lambda e: e.name)
            yield current, media_entries

        for sd in sorted(subdirs, reverse=True):
            stack.append(sd)


def _resolve_link_target(base_dir: Path, raw_target: str) -> Path:
    """Resolve a (possibly broken) symlink target relative to `base_dir` portably across OSes."""
    cleaned = raw_target[4:] if raw_target.startswith("\\\\?\\") else raw_target
    raw = Path(cleaned)
    combined = raw if raw.is_absolute() else Path(os.path.normpath(str(base_dir / raw)))
    try:
        return combined.resolve(strict=False)
    except OSError:
        return combined


def _find_canonical_for_orphan(
    link_path: Path,
    library_by_short_key: dict[tuple[str, str], list[Path]],
    library_root: Path,
) -> Optional[Path]:
    """Try to locate the canonical `_library/YYYY/MM/` file for a broken symlink or orphan hardlink."""
    if link_path.is_symlink():
        try:
            raw_target = os.readlink(link_path)
            resolved = _resolve_link_target(link_path.parent, raw_target)
            if resolved.is_relative_to(library_root):
                v = parse_variant(resolved, size=0)
                cand_dir = resolved.parent
                if cand_dir.is_dir():
                    for ext in (resolved.suffix, v.ext_key, ".jpg", ".JPG", ".jpeg"):
                        if not ext or ext.lower() in {".modifié", ".modifie"}:
                            continue
                        cand = cand_dir / f"{v.base_stem}{ext}"
                        if cand.is_file():
                            return cand
        except OSError:
            pass

    v = parse_variant(link_path, size=0)
    candidates = library_by_short_key.get((v.base_stem.lower(), v.ext_key), [])
    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1 and not link_path.is_symlink():
        try:
            dt = time.localtime(link_path.stat().st_mtime)
            ym = (f"{dt.tm_year:04d}", f"{dt.tm_mon:02d}")
            ym_matches = [
                c
                for c in candidates
                if len(c.relative_to(library_root).parts) >= 2
                and c.relative_to(library_root).parts[:2] == ym
            ]
            if len(ym_matches) == 1:
                return ym_matches[0]
        except OSError:
            pass

    return None


def deduplicate_library_variants(
    root_dir: str | Path = DEFAULT_ROOT_DIR,
    library_dirname: str = "_library",
    albums_dirname: str = "albums",
    dry_run: bool = False,
    rename_standalone: bool = False,
    skip_albums: bool = False,
    albums_only: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """Phase 1: deduplicate & rename in `_library`. Phase 2: check & repair `albums/` sequentially."""
    root = Path(root_dir).expanduser().resolve()
    library_root = root / library_dirname
    albums_root = root / albums_dirname

    if not library_root.exists():
        raise FileNotFoundError(f"Library directory does not exist: {library_root}")

    progress = _Progress(verbose=verbose)
    t0 = time.monotonic()

    stats = {
        "groups_processed": 0,
        "files_deleted": 0,
        "files_renamed": 0,
        "distinct_preserved": 0,
        "bytes_freed": 0,
        "albums_scanned": 0,
        "album_files_checked": 0,
        "album_links_updated": 0,
        "album_links_removed": 0,
        "broken_links_repaired": 0,
    }

    inode_redirect: dict[tuple[int, int], tuple[Path, tuple[int, int]]] = {}
    path_redirect: dict[Path, tuple[Path, tuple[int, int]]] = {}
    library_by_short_key: dict[tuple[str, str], list[Path]] = defaultdict(list)

    # =========================================================================
    # PHASE 1: Deduplicate and rename directly inside `_library/YYYY/MM/`
    # =========================================================================
    mode_tag = " (DRY-RUN)" if dry_run else ""
    if not albums_only:
        progress.log(f"[Phase 1/2] Deduplicating & renaming in {library_root}{mode_tag}...")
    else:
        progress.log(f"[Phase 1/2] Indexing {library_root} (albums-only mode)...")

    for walk_root, dirnames, filenames in os.walk(library_root, topdown=True):
        dirnames.sort()
        current_dir = Path(walk_root)
        if not filenames:
            continue

        rel_dir = str(current_dir.relative_to(library_root))
        progress.update(
            f"P1 _library {rel_dir} | grp: {stats['groups_processed']} | "
            f"del: {stats['files_deleted']} ({_format_bytes(stats['bytes_freed'])}) | "
            f"ren: {stats['files_renamed']}"
        )

        # Group media files purely by filename strings (ZERO stat() syscalls for non-duplicate files!)
        raw_groups: dict[tuple[str, str], list[VariantInfo]] = defaultdict(list)
        for fname in sorted(filenames):
            ext_low = os.path.splitext(fname)[1].lower()
            if ext_low not in SUPPORTED_EXTENSIONS and ext_low not in {".modifié", ".modifie"}:
                continue
            p = current_dir / fname
            info = parse_variant(p, size=0)
            raw_groups[(info.base_stem.lower(), info.ext_key)].append(info)

        if albums_only:
            for (short_key, ext_key), items in raw_groups.items():
                for it in items:
                    library_by_short_key[(short_key, ext_key)].append(it.path)
            continue

        for (short_key, ext_key), items in sorted(raw_groups.items()):
            has_variant = any(it.kind in VARIANT_KINDS for it in items)
            if not has_variant:
                for it in items:
                    library_by_short_key[(short_key, ext_key)].append(it.path)
                continue

            if len(items) == 1 and not rename_standalone:
                library_by_short_key[(short_key, ext_key)].append(items[0].path)
                continue

            # Populate stat sizes only for actual candidate groups
            valid_items: list[VariantInfo] = []
            for it in items:
                try:
                    st = it.path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    it.size = st.st_size
                    valid_items.append(it)
            if not valid_items:
                continue

            subgroups = _cluster_group(valid_items)
            if len(subgroups) > 1:
                stats["distinct_preserved"] += sum(
                    1 for cl, allow in subgroups if not allow
                )

            for cluster, allow_short_rename in subgroups:
                if not any(it.kind in VARIANT_KINDS for it in cluster):
                    for it in cluster:
                        library_by_short_key[(short_key, ext_key)].append(it.path)
                    continue

                if len(cluster) == 1:
                    only = cluster[0]
                    if not (
                        rename_standalone
                        and allow_short_rename
                        and only.kind in {"original_o", "modified"}
                    ):
                        library_by_short_key[(short_key, ext_key)].append(only.path)
                        continue

                winner = _choose_winner(cluster)
                losers = [it for it in cluster if it.path != winner.path]

                if allow_short_rename:
                    plain_items = [it for it in cluster if it.kind == "plain"]
                    if plain_items:
                        target_name = plain_items[0].path.name
                    else:
                        target_ext = (
                            winner.path.suffix
                            if winner.path.suffix.lower() not in {".modifié", ".modifie"}
                            else ext_key
                        )
                        target_name = f"{winner.base_stem}{target_ext}"
                else:
                    target_name = winner.path.name

                target_path = current_dir / target_name
                cluster_paths = {it.path for it in cluster}

                if target_path.exists() and target_path not in cluster_paths:
                    if verbose:
                        progress.log(
                            f"[Phase 1] SKIP conflict: {target_path.relative_to(root)} already exists"
                        )
                    for it in cluster:
                        library_by_short_key[(short_key, ext_key)].append(it.path)
                    continue

                if not losers and winner.path == target_path:
                    library_by_short_key[(short_key, ext_key)].append(winner.path)
                    continue

                stats["groups_processed"] += 1

                # Record inode & path redirects BEFORE modifying disk
                winner_st = winner.path.stat()
                winner_ino = (winner_st.st_dev, winner_st.st_ino)
                if winner.path != target_path:
                    inode_redirect[winner_ino] = (target_path, winner_ino)
                    path_redirect[winner.path.resolve()] = (target_path, winner_ino)
                for l_it in losers:
                    l_st = l_it.path.stat()
                    l_ino = (l_st.st_dev, l_st.st_ino)
                    inode_redirect[l_ino] = (target_path, winner_ino)
                    path_redirect[l_it.path.resolve()] = (target_path, winner_ino)

                if verbose:
                    tag = "[DRY-RUN] " if dry_run else ""
                    loser_names = ", ".join(it.path.name for it in losers) or "(none)"
                    progress.log(
                        f"{tag}[_library] {rel_dir}: KEEP {winner.path.name} -> {target_name} "
                        f"| DELETE [{loser_names}]"
                    )

                for l_it in losers:
                    stats["files_deleted"] += 1
                    stats["bytes_freed"] += l_it.size
                    if not dry_run:
                        l_it.path.unlink()

                if winner.path != target_path:
                    stats["files_renamed"] += 1
                    if not dry_run:
                        winner.path.rename(target_path)

                library_by_short_key[(short_key, ext_key)].append(target_path)

    progress.clear()
    t_phase1 = time.monotonic() - t0
    progress.log(
        f"[Phase 1/2] Done in {t_phase1:.1f}s — groups: {stats['groups_processed']}, "
        f"deleted: {stats['files_deleted']} ({_format_bytes(stats['bytes_freed'])}), "
        f"renamed: {stats['files_renamed']}, "
        f"distinct photos preserved: {stats['distinct_preserved']}."
    )

    # =========================================================================
    # PHASE 2: Walk `albums/` sequentially and restore broken / outdated links
    # =========================================================================
    if skip_albums or not albums_root.exists():
        return stats

    t2 = time.monotonic()
    progress.log(f"[Phase 2/2] Checking albums one by one in {albums_root}{mode_tag}...")

    for album_dir, media_entries in _iter_album_dirs(albums_root):
        stats["albums_scanned"] += 1
        rel_album = str(album_dir.relative_to(albums_root))

        album_repairs: dict[Path, tuple[Optional[tuple[int, int]], list[tuple[Path, bool, bool]]]] = {}

        for entry in media_entries:
            stats["album_files_checked"] += 1
            link_path = Path(entry.path)

            try:
                is_sym = entry.is_symlink()
            except OSError:
                continue

            target_canonical: Optional[Path] = None
            winner_ino: Optional[tuple[int, int]] = None
            is_orphan_or_broken = False

            if is_sym:
                try:
                    raw_target = os.readlink(link_path)
                    resolved_target = _resolve_link_target(album_dir, raw_target)
                except OSError:
                    continue

                if resolved_target in path_redirect:
                    target_canonical, winner_ino = path_redirect[resolved_target]
                elif not link_path.exists():
                    found = _find_canonical_for_orphan(link_path, library_by_short_key, library_root)
                    if found is not None:
                        target_canonical = found
                        try:
                            st_f = found.stat()
                            winner_ino = (st_f.st_dev, st_f.st_ino)
                        except OSError:
                            pass
                        is_orphan_or_broken = True
            else:
                try:
                    # Use os.lstat() rather than DirEntry.stat(): on Windows, DirEntry.stat()
                    # leaves st_dev, st_ino, and st_nlink as 0.
                    st = link_path.lstat()
                except OSError:
                    continue
                ino = (st.st_dev, st.st_ino)

                if ino in inode_redirect:
                    target_canonical, winner_ino = inode_redirect[ino]
                elif st.st_nlink == 1:
                    found = _find_canonical_for_orphan(link_path, library_by_short_key, library_root)
                    if found is not None:
                        target_canonical = found
                        try:
                            st_f = found.stat()
                            winner_ino = (st_f.st_dev, st_f.st_ino)
                        except OSError:
                            pass
                        is_orphan_or_broken = True

            if target_canonical is not None:
                if target_canonical not in album_repairs:
                    album_repairs[target_canonical] = (winner_ino, [])
                album_repairs[target_canonical][1].append((link_path, is_sym, is_orphan_or_broken))

        for target_canonical, (winner_ino, old_items) in sorted(album_repairs.items()):
            old_paths = [p for p, _, _ in old_items]
            use_symlink = all(is_sym for _, is_sym, _ in old_items)
            had_broken_orphan = any(is_ob for _, _, is_ob in old_items)
            desired_link = album_dir / target_canonical.name

            if (desired_link.exists() or desired_link.is_symlink()) and desired_link not in old_paths:
                same_target = False
                try:
                    st_d = desired_link.stat()
                    if winner_ino is not None and (st_d.st_dev, st_d.st_ino) == winner_ino:
                        same_target = True
                except OSError:
                    pass
                if same_target:
                    for old_lp in old_paths:
                        stats["album_links_removed"] += 1
                        if had_broken_orphan:
                            stats["broken_links_repaired"] += 1
                        if verbose:
                            tag = "[DRY-RUN] " if dry_run else ""
                            progress.log(
                                f"{tag}[album] {rel_album}: REMOVE duplicate link {old_lp.name} "
                                f"(already linked as {desired_link.name})"
                            )
                        if not dry_run:
                            try:
                                old_lp.unlink()
                            except OSError:
                                pass
                    continue
                else:
                    desired_link = old_paths[0]

            already_ok = False
            if desired_link in old_paths and not use_symlink and winner_ino is not None:
                try:
                    st_d = desired_link.stat()
                    if not desired_link.is_symlink() and (st_d.st_dev, st_d.st_ino) == winner_ino:
                        already_ok = True
                except OSError:
                    pass

            for old_lp in old_paths:
                if already_ok and old_lp == desired_link:
                    continue
                stats["album_links_removed"] += 1
                if not dry_run:
                    try:
                        old_lp.unlink()
                    except OSError:
                        pass

            if not already_ok:
                stats["album_links_updated"] += 1
                if had_broken_orphan:
                    stats["broken_links_repaired"] += 1
                if verbose:
                    tag = "[DRY-RUN] " if dry_run else ""
                    old_names = ", ".join(p.name for p in old_paths)
                    progress.log(
                        f"{tag}[album] {rel_album}: [{old_names}] -> {desired_link.name}"
                    )
                if not dry_run:
                    if use_symlink:
                        rel_target = os.path.relpath(target_canonical, start=album_dir)
                        os.symlink(rel_target, desired_link)
                    else:
                        try:
                            os.link(target_canonical, desired_link)
                        except OSError:
                            rel_target = os.path.relpath(target_canonical, start=album_dir)
                            os.symlink(rel_target, desired_link)

        progress.update(
            f"P2 albums | dirs: {stats['albums_scanned']} | "
            f"checked: {stats['album_files_checked']} | "
            f"updated: {stats['album_links_updated']} | {rel_album}"
        )

    progress.clear()
    t_total = time.monotonic() - t0
    t_phase2 = time.monotonic() - t2
    progress.log(
        f"[Dedup] Done{mode_tag}! (total {t_total:.1f}s: _library {t_phase1:.1f}s, albums {t_phase2:.1f}s)\n"
        f"  Duplicate groups processed    : {stats['groups_processed']}\n"
        f"  Redundant files deleted       : {stats['files_deleted']} ({_format_bytes(stats['bytes_freed'])})\n"
        f"  Winner files renamed to short : {stats['files_renamed']}\n"
        f"  Distinct photos preserved     : {stats['distinct_preserved']}\n"
        f"  Albums scanned                : {stats['albums_scanned']} ({stats['album_files_checked']} media links)\n"
        f"  Album links updated/rebuilt   : {stats['album_links_updated']} (orphans/broken repaired: {stats['broken_links_repaired']})\n"
        f"  Old album links removed       : {stats['album_links_removed']}"
    )
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 1: Deduplicate `_<id>_o`, `-modifié`, `_<8hex>`, and `(N)` image variants in "
            "`_library/YYYY/MM/`, keeping the highest-priority / highest-quality copy and renaming "
            "it to the short filename. Phase 2: Check `albums/` one by one and restore broken or outdated links."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=DEFAULT_ROOT_DIR,
        help=f"Crosspix root directory containing `_library/` and `albums/` (default: {DEFAULT_ROOT_DIR}).",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview all deletions, renames, and album link repairs without modifying disk.",
    )
    parser.add_argument(
        "--rename-standalone",
        action="store_true",
        help="Also rename single standalone `_<id>_o` or `-modifié` files (with no duplicate) to their short name.",
    )
    parser.add_argument(
        "--skip-albums",
        action="store_true",
        help="Run only Phase 1 (`_library` deduplication and renaming) without scanning `albums/`.",
    )
    parser.add_argument(
        "--albums-only",
        action="store_true",
        help="Skip Phase 1 changes and run only Phase 2 to repair broken/orphaned links in `albums/`.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print every individual group and album link change.",
    )

    args = parser.parse_args(argv)
    try:
        deduplicate_library_variants(
            root_dir=args.root,
            dry_run=args.dry_run,
            rename_standalone=args.rename_standalone,
            skip_albums=args.skip_albums,
            albums_only=args.albums_only,
            verbose=args.verbose,
        )
        return 0
    except KeyboardInterrupt:
        print("\n[Dedup] Interrupted.", flush=True)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
