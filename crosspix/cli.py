"""Command-line interface for crosspix."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from crosspix.base import PhotoProvider
from crosspix.ente import EnteProvider
from crosspix.google_photos import GooglePhotosProvider
from crosspix.immich import ImmichProvider
from crosspix.local import LocalProvider
from crosspix.sync import PhotoSynchronizer
from crosspix.youtube import YouTubeProvider


def _read_key_file(path_str: str) -> str:
    """Read an API key from a local file, ignoring blank lines and `#` comments."""
    if not path_str:
        return ""
    path = os.path.expanduser(path_str)
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line and line.split("=", 1)[0].strip().upper() == "IMMICH_API_KEY":
                line = line.split("=", 1)[1].strip().strip("\"'")
            if line:
                return line
    return ""


def build_provider(spec: str, args: argparse.Namespace) -> PhotoProvider:
    """Instantiate a PhotoProvider from a specification string like 'local:/path' or 'immich'."""
    if ":" in spec:
        kind, value = spec.split(":", 1)
    else:
        kind, value = spec, ""

    kind = kind.strip().lower()
    if kind == "local":
        path = value or args.local_dir
        if not path:
            raise ValueError("Local provider requires a path (e.g. 'local:/path/to/photos').")
        return LocalProvider(
            root_dir=path,
            recursive=not getattr(args, "no_recursive", False),
            album_delimiter=args.album_delimiter,
            link_mode=args.link_mode,
            albums_only=args.albums_only,
        )

    if kind in {"google", "google_photos", "gphotos", "google_takeout"}:
        if value and os.path.isdir(os.path.expanduser(value)):
            return GooglePhotosProvider(takeout_dir=value)
        secret_file = value or args.google_secret
        return GooglePhotosProvider(
            client_secret_file=secret_file,
            access_token=os.environ.get("GOOGLE_PHOTOS_TOKEN"),
            takeout_dir=args.google_takeout,
        )

    if kind == "ente":
        export_dir = value or args.ente_export_dir
        return EnteProvider(
            api_url=args.ente_api_url,
            auth_token=args.ente_token or os.environ.get("ENTE_AUTH_TOKEN"),
            export_dir=export_dir,
            album_delimiter=args.album_delimiter,
            link_mode=args.link_mode,
            run_cli_export=args.ente_cli_sync,
            account=args.ente_account,
        )

    if kind == "immich":
        base_url = (
            value
            or args.immich_url
            or os.environ.get("IMMICH_URL", "http://localhost:2283/api")
        )
        api_key = (
            args.immich_key
            or os.environ.get("IMMICH_API_KEY", "")
            or _read_key_file(getattr(args, "immich_key_file", "immich.token"))
        )
        return ImmichProvider(base_url=base_url, api_key=api_key)

    if kind in {"youtube", "yt"}:
        secret_file = value or getattr(args, "youtube_secret", "youtube_oauth.json")
        return YouTubeProvider(
            client_secret_file=secret_file,
            fallback_secret_file=getattr(args, "google_secret", "gphoto_oauth.json"),
            token_file=getattr(args, "youtube_token", "youtube.token"),
            privacy_status=getattr(args, "youtube_privacy", "private"),
        )

    raise ValueError(
        f"Unknown platform '{kind}'. Supported platforms: local, google_photos, ente, immich, youtube."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crosspix",
        description=(
            "Synchronize photos across Google Photos, Ente Photos, Immich, and local folders."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--album", help="Optional album/collection name filter.")
    common.add_argument("--local-dir", help="Default local folder path.")
    common.add_argument(
        "--album-delimiter",
        default=" ",
        help="Delimiter used to map flat remote album names to local folder hierarchies (default: ' ').",
    )
    common.add_argument(
        "--link-mode",
        choices=["hardlink", "symlink", "copy"],
        default="hardlink",
        help="How album folders link to canonical files in _library/YYYY/MM/ (default: hardlink).",
    )
    common.add_argument(
        "--albums-only",
        action="store_true",
        help="For local sources, scan only the albums/ directory and ignore unlinked files in _library/.",
    )
    common.add_argument(
        "--no-recursive",
        action="store_true",
        help="For local sources, scan only the specified directory without descending into subdirectories.",
    )
    common.add_argument(
        "--google-secret",
        default="gphoto_oauth.json",
        help="Path to Google Photos OAuth client secret JSON file.",
    )
    common.add_argument(
        "--google-takeout",
        help="Path to an extracted Google Takeout directory (imports all photos and all albums).",
    )
    common.add_argument(
        "--ente-api-url",
        default="https://api.ente.io",
        help="Ente Museum API URL.",
    )
    common.add_argument("--ente-token", help="Ente API authentication token.")
    common.add_argument("--ente-export-dir", help="Local Ente Desktop / ente-cli export directory.")
    common.add_argument("--ente-account", help="Optional account name for ente-cli (--account NAME).")
    common.add_argument(
        "--ente-cli-sync",
        action="store_true",
        help="Automatically run 'ente photos export DIR' via ente-cli before synchronizing.",
    )
    common.add_argument(
        "--immich-url",
        default="http://localhost:2283/api",
        help="Immich API base URL.",
    )
    common.add_argument("--immich-key", help="Immich API key.")
    common.add_argument(
        "--immich-key-file",
        default="immich.token",
        help="Path to a gitignored file containing the Immich API key (default: immich.token).",
    )
    common.add_argument(
        "--youtube-secret",
        default="youtube_oauth.json",
        help="Path to YouTube OAuth client secret JSON file (falls back to --google-secret).",
    )
    common.add_argument(
        "--youtube-token",
        default="youtube.token",
        help="Path to cached YouTube OAuth token file (default: youtube.token, gitignored).",
    )
    common.add_argument(
        "--youtube-privacy",
        choices=["private", "unlisted", "public"],
        default="private",
        help="Privacy status for uploaded YouTube videos and created playlists (default: private).",
    )

    list_parser = subparsers.add_parser(
        "list", parents=[common], help="List photos on a provider."
    )
    list_parser.add_argument(
        "provider",
        help="Provider spec (e.g. 'local:/path/to/photos', 'google_photos', 'ente', 'immich').",
    )

    sync_parser = subparsers.add_parser(
        "sync", parents=[common], help="Synchronize photos from source to target."
    )
    sync_parser.add_argument("source", help="Source provider specification.")
    sync_parser.add_argument("target", help="Target provider specification.")
    sync_parser.add_argument(
        "--bidirectional",
        "-b",
        action="store_true",
        help="Perform bi-directional synchronization between source and target.",
    )
    sync_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Simulate synchronization without uploading or modifying files.",
    )
    sync_parser.add_argument(
        "--max-video-size-mb",
        type=float,
        default=None,
        help=(
            "Skip videos larger than this size in MB (default: 100 MB for non-YouTube targets; "
            "pass 0 for unlimited)."
        ),
    )
    sync_parser.add_argument(
        "--min-video-size-mb",
        type=float,
        default=None,
        help="Only synchronize videos at least this size in MB (e.g. --min-video-size-mb 100 when syncing to youtube).",
    )
    sync_parser.add_argument(
        "--no-albums",
        action="store_true",
        help=(
            "Synchronize media files only without creating or linking albums/folders/playlists "
            "on the target."
        ),
    )
    sync_parser.add_argument(
        "--images-only",
        action="store_true",
        help="Synchronize image files only and skip all videos.",
    )
    sync_parser.add_argument(
        "--error-log",
        default="error.log",
        help="Path to the error log file where skipped file errors are recorded (default: error.log).",
    )
    sync_parser.add_argument(
        "--youtube-large-videos",
        action="store_true",
        help=(
            "Automatically upload videos exceeding --max-video-size-mb to YouTube "
            "(organized into playlists matching album names) instead of skipping them."
        ),
    )

    return parser


def _log(msg: str) -> None:
    print(msg, flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            provider = build_provider(args.provider, args)
            _log(f"Connecting to {provider.platform_name}...")
            provider.connect()
            count = 0
            for item in provider.list_photos(album=args.album):
                count += 1
                dt_str = item.created_at.isoformat() if item.created_at else "unknown-date"
                albums_str = f", albums={item.albums}" if item.albums else ""
                _log(
                    f"[{item.platform}] {item.filename} (id={item.id}, date={dt_str}{albums_str})"
                )
            _log(f"Total photos listed: {count}")
            return 0

        if args.command == "sync":
            source = build_provider(args.source, args)
            target = build_provider(args.target, args)

            # Default max video size to 100 MB for non-YouTube targets (0 disables the limit)
            if args.max_video_size_mb is None:
                max_video_mb: Optional[float] = (
                    None if target.platform_name == "youtube" else 100.0
                )
            elif args.max_video_size_mb <= 0:
                max_video_mb = None
            else:
                max_video_mb = args.max_video_size_mb

            large_video_target = (
                build_provider("youtube", args)
                if (args.youtube_large_videos and target.platform_name != "youtube")
                else None
            )

            synchronizer = PhotoSynchronizer(
                dry_run=args.dry_run,
                on_progress=_log,
                max_video_size_mb=max_video_mb,
                min_video_size_mb=args.min_video_size_mb,
                large_video_target=large_video_target,
                sync_albums=not args.no_albums,
                images_only=args.images_only,
                error_log_path=args.error_log,
            )

            if args.bidirectional:
                res_ab, res_ba = synchronizer.bisync(source, target, album=args.album)
                _log(
                    f"Bi-sync complete: {len(res_ab.copied)} copied {source.platform_name}->{target.platform_name}, "
                    f"{len(res_ba.copied)} copied {target.platform_name}->{source.platform_name}."
                )
            else:
                res = synchronizer.sync(source, target, album=args.album)
                extra_parts = []
                if res.skipped_large_videos:
                    extra_parts.append(f"{len(res.skipped_large_videos)} large videos skipped")
                if res.routed_to_youtube and target.platform_name != "youtube":
                    extra_parts.append(f"{len(res.routed_to_youtube)} large videos -> youtube")
                extra_str = f" ({', '.join(extra_parts)})" if extra_parts else ""
                _log(
                    f"Sync complete: {len(res.copied)} copied, "
                    f"{len(res.albums_updated)} album-linked, "
                    f"{len(res.skipped)} skipped, "
                    f"{len(res.errors)} errors{extra_str}."
                )
                if res.errors and args.error_log:
                    _log(f"Errors logged to: {args.error_log}")
            return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command to resume from where it left off.", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
