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
        return LocalProvider(root_dir=path)

    if kind in {"google", "google_photos", "gphotos"}:
        secret_file = value or args.google_secret
        return GooglePhotosProvider(
            client_secret_file=secret_file,
            access_token=os.environ.get("GOOGLE_PHOTOS_TOKEN"),
        )

    if kind == "ente":
        export_dir = value or args.ente_export_dir
        return EnteProvider(
            api_url=args.ente_api_url,
            auth_token=args.ente_token or os.environ.get("ENTE_AUTH_TOKEN"),
            export_dir=export_dir,
        )

    if kind == "immich":
        base_url = value or args.immich_url or os.environ.get("IMMICH_URL", "http://localhost:2283/api")
        api_key = args.immich_key or os.environ.get("IMMICH_API_KEY", "")
        return ImmichProvider(base_url=base_url, api_key=api_key)

    raise ValueError(
        f"Unknown platform '{kind}'. Supported platforms: local, google_photos, ente, immich."
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
        "--google-secret",
        default="gphoto_oauth.json",
        help="Path to Google Photos OAuth client secret JSON file.",
    )
    common.add_argument(
        "--ente-api-url",
        default="https://api.ente.io",
        help="Ente Museum API URL.",
    )
    common.add_argument("--ente-token", help="Ente API authentication token.")
    common.add_argument("--ente-export-dir", help="Local Ente CLI export directory.")
    common.add_argument(
        "--immich-url",
        default="http://localhost:2283/api",
        help="Immich API base URL.",
    )
    common.add_argument("--immich-key", help="Immich API key.")

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

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            provider = build_provider(args.provider, args)
            provider.connect()
            count = 0
            for item in provider.list_photos(album=args.album):
                count += 1
                dt_str = item.created_at.isoformat() if item.created_at else "unknown-date"
                print(f"[{item.platform}] {item.filename} (id={item.id}, date={dt_str})")
            print(f"Total photos listed: {count}")
            return 0

        if args.command == "sync":
            source = build_provider(args.source, args)
            target = build_provider(args.target, args)
            synchronizer = PhotoSynchronizer(dry_run=args.dry_run, on_progress=print)

            if args.bidirectional:
                res_ab, res_ba = synchronizer.bisync(source, target, album=args.album)
                print(
                    f"Bi-sync complete: {len(res_ab.copied)} copied {source.platform_name}->{target.platform_name}, "
                    f"{len(res_ba.copied)} copied {target.platform_name}->{source.platform_name}."
                )
            else:
                res = synchronizer.sync(source, target, album=args.album)
                print(
                    f"Sync complete: {len(res.copied)} copied, {len(res.skipped)} skipped, "
                    f"{len(res.errors)} errors."
                )
            return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
