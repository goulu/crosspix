"""Immich provider using immichpy and httpx with multi-album deduplication support."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
from immichpy import AsyncClient
from immichpy.client.generated import MetadataSearchDto

from crosspix.base import PhotoItem, PhotoProvider


class ImmichProvider(PhotoProvider):
    """Photo provider for synchronizing photos and multi-album memberships with an Immich server."""

    platform_name = "immich"

    def __init__(
        self,
        base_url: str = "http://localhost:2283/api",
        api_key: str = "",
        http_client: Optional[httpx.Client] = None,
        include_albums: bool = True,
    ) -> None:
        normalized = base_url.rstrip("/")
        if not normalized.endswith("/api"):
            normalized = f"{normalized}/api"
        self.base_url = normalized
        self.api_key = api_key
        self.http_client = http_client or httpx.Client(timeout=300.0)
        self.include_albums = include_albums
        self._album_cache: dict[str, str] = {}
        self._album_assets_cache: dict[str, set[str]] = {}
        self._albums_prefetched: bool = False

    def connect(self) -> None:
        """Verify configuration for the Immich server."""
        if not self.api_key:
            raise ValueError("ImmichProvider requires a non-empty 'api_key'.")

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Accept": "application/json",
        }

    def _create_async_client(self) -> AsyncClient:
        return AsyncClient(api_key=self.api_key, base_url=self.base_url)

    @staticmethod
    def _asset_to_photo_item(
        asset: Any,
        album: Optional[str] = None,
        albums: Optional[list[str]] = None,
    ) -> PhotoItem:
        if isinstance(asset, dict):
            asset_id = str(asset.get("id", ""))
            filename = str(
                asset.get("originalFileName")
                or asset.get("original_file_name")
                or f"{asset_id}.jpg"
            )
            checksum = asset.get("checksum")
            created_raw = asset.get("fileCreatedAt") or asset.get("file_created_at")
            mime_type = str(
                asset.get("originalMimeType")
                or asset.get("original_mime_type")
                or "image/jpeg"
            )
            raw_meta = asset
        else:
            asset_id = str(getattr(asset, "id", ""))
            filename = str(
                getattr(asset, "original_file_name", None)
                or getattr(asset, "originalFileName", None)
                or f"{asset_id}.jpg"
            )
            checksum = getattr(asset, "checksum", None)
            created_raw = getattr(asset, "file_created_at", None) or getattr(
                asset, "fileCreatedAt", None
            )
            mime_type = str(
                getattr(asset, "original_mime_type", None)
                or getattr(asset, "originalMimeType", None)
                or "image/jpeg"
            )
            raw_meta = (
                asset.model_dump()
                if hasattr(asset, "model_dump")
                else {"id": asset_id, "filename": filename}
            )

        created_at: Optional[datetime] = None
        if isinstance(created_raw, datetime):
            created_at = created_raw.replace(tzinfo=None)
        elif isinstance(created_raw, str):
            try:
                created_at = datetime.fromisoformat(
                    created_raw.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except ValueError:
                pass

        all_albums = list(albums) if albums else []
        if album and album not in all_albums:
            all_albums.append(album)

        return PhotoItem(
            id=asset_id,
            filename=filename,
            platform="immich",
            checksum=checksum,
            created_at=created_at,
            mime_type=mime_type,
            album=all_albums[0] if all_albums else album,
            albums=all_albums,
            raw_metadata=raw_meta,
        )

    def prefetch_albums(self) -> dict[str, str]:
        """Fetch and cache all existing Immich albums in a single request."""
        self.connect()
        resp = self.http_client.get(
            f"{self.base_url}/albums",
            headers=self._headers(),
        )
        resp.raise_for_status()
        for alb in resp.json() or []:
            if isinstance(alb, dict):
                title = alb.get("albumName") or alb.get("album_name")
                alb_id = alb.get("id")
                if title and alb_id:
                    self._album_cache[str(title)] = str(alb_id)
        self._albums_prefetched = True
        return dict(self._album_cache)

    def get_album_asset_ids(self, album_id: str) -> set[str]:
        """Return (and cache) the set of asset UUIDs currently in an Immich album."""
        if album_id in self._album_assets_cache:
            return self._album_assets_cache[album_id]
        resp = self.http_client.get(
            f"{self.base_url}/albums/{album_id}",
            headers=self._headers(),
            params={"withoutAssets": "false"},
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        assets = payload.get("assets") or []
        ids = {
            str(a.get("id"))
            for a in assets
            if isinstance(a, dict) and a.get("id")
        }
        self._album_assets_cache[album_id] = ids
        return ids

    def check_bulk_upload(
        self, items: list[tuple[str, str]]
    ) -> dict[str, tuple[str, Optional[str]]]:
        """Check a batch of `(client_id, sha1_checksum)` pairs against Immich.

        Returns `{client_id: (action, existing_asset_id)}` where `action` is
        typically `"accept"` or `"reject"` (with `existing_asset_id` set on duplicates).
        """
        self.connect()
        if not items:
            return {}
        resp = self.http_client.post(
            f"{self.base_url}/assets/bulk-upload-check",
            headers=self._headers(),
            json={
                "assets": [
                    {"id": client_id, "checksum": checksum}
                    for client_id, checksum in items
                ]
            },
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        out: dict[str, tuple[str, Optional[str]]] = {}
        for entry in payload.get("results") or []:
            if not isinstance(entry, dict):
                continue
            cid = str(entry.get("id", ""))
            action = str(entry.get("action", "accept")).lower()
            asset_id = entry.get("assetId") or entry.get("asset_id")
            out[cid] = (action, str(asset_id) if asset_id else None)
        return out

    def _resolve_album_id(self, album_name: str, create_if_missing: bool = False) -> Optional[str]:
        """Resolve an Immich album title to its UUID, optionally creating it if missing."""
        if album_name in self._album_cache:
            return self._album_cache[album_name]

        if not self._albums_prefetched:
            resp = self.http_client.get(
                f"{self.base_url}/albums",
                headers=self._headers(),
            )
            resp.raise_for_status()
            for alb in resp.json() or []:
                if isinstance(alb, dict):
                    title = alb.get("albumName") or alb.get("album_name")
                    alb_id = alb.get("id")
                    if title and alb_id:
                        self._album_cache[str(title)] = str(alb_id)

        if album_name in self._album_cache:
            return self._album_cache[album_name]

        if create_if_missing:
            create_resp = self.http_client.post(
                f"{self.base_url}/albums",
                headers=self._headers(),
                json={"albumName": album_name},
            )
            create_resp.raise_for_status()
            alb_id = create_resp.json().get("id")
            if alb_id:
                alb_id_str = str(alb_id)
                self._album_cache[album_name] = alb_id_str
                self._album_assets_cache.setdefault(alb_id_str, set())
                return alb_id_str

        return None

    def _add_asset_to_album(self, asset_id: str, album_id: str) -> None:
        """Attach an existing Immich asset ID to an album without re-uploading."""
        self.add_assets_to_album(album_id, [asset_id])

    def add_assets_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        """Attach one or more Immich asset IDs to an album without re-uploading."""
        ids = [aid for aid in asset_ids if aid]
        if not album_id or not ids:
            return
        resp = self.http_client.put(
            f"{self.base_url}/albums/{album_id}/assets",
            headers=self._headers(),
            json={"ids": ids},
        )
        resp.raise_for_status()
        if album_id in self._album_assets_cache:
            self._album_assets_cache[album_id].update(ids)

    async def _list_photos_async(self, album: Optional[str] = None) -> list[PhotoItem]:
        async with self._create_async_client() as client:
            if album:
                albums = await client.album.get_all_albums()
                for alb in albums or []:
                    alb_name = getattr(alb, "album_name", None) or (
                        alb.get("albumName") if isinstance(alb, dict) else None
                    )
                    alb_id = getattr(alb, "id", None) or (
                        alb.get("id") if isinstance(alb, dict) else None
                    )
                    if alb_name and alb_id:
                        self._album_cache[str(alb_name)] = str(alb_id)
                    if alb_name == album and alb_id:
                        album_info = await client.album.get_album_info(id=alb_id)
                        assets = getattr(album_info, "assets", []) or []
                        return [self._asset_to_photo_item(a, album=album) for a in assets]
                return []

            items_by_id: dict[str, PhotoItem] = {}

            page = 1
            while True:
                query = MetadataSearchDto(size=1000, page=page)
                results = await client.search.search_assets(metadata_search_dto=query)
                assets_obj = getattr(results, "assets", results)
                items = getattr(assets_obj, "items", assets_obj)
                if isinstance(items, list):
                    for a in items:
                        photo_item = self._asset_to_photo_item(a)
                        if photo_item.id:
                            items_by_id[photo_item.id] = photo_item
                next_page = getattr(assets_obj, "next_page", None) or getattr(
                    assets_obj, "nextPage", None
                )
                if not next_page:
                    break
                try:
                    page = int(next_page)
                except (TypeError, ValueError):
                    break

            if self.include_albums:
                albums = await client.album.get_all_albums()
                for alb in albums or []:
                    alb_name = getattr(alb, "album_name", None) or (
                        alb.get("albumName") if isinstance(alb, dict) else None
                    )
                    alb_id = getattr(alb, "id", None) or (
                        alb.get("id") if isinstance(alb, dict) else None
                    )
                    if not alb_name or not alb_id:
                        continue
                    self._album_cache[str(alb_name)] = str(alb_id)
                    album_info = await client.album.get_album_info(id=alb_id)
                    assets = getattr(album_info, "assets", []) or []
                    for a in assets:
                        parsed = self._asset_to_photo_item(a, album=str(alb_name))
                        if parsed.id in items_by_id:
                            items_by_id[parsed.id].add_album(str(alb_name))
                        elif parsed.id:
                            items_by_id[parsed.id] = parsed

            return list(items_by_id.values())

    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """List deduplicated PhotoItems from Immich with all their album memberships."""
        self.connect()
        return asyncio.run(self._list_photos_async(album=album))

    def download_photo(self, item: PhotoItem, destination_dir: Path) -> Path:
        """Download an original asset from Immich into destination_dir."""
        self.connect()
        destination_dir = Path(destination_dir).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)

        response = self.http_client.get(
            f"{self.base_url}/assets/{item.id}/original",
            headers=self._headers(),
        )
        response.raise_for_status()

        target_path = destination_dir / item.filename
        target_path.write_bytes(response.content)
        return target_path

    def upload_photo(
        self,
        file_path: Path,
        item: Optional[PhotoItem] = None,
        album: Optional[str] = None,
    ) -> PhotoItem:
        """Upload a local photo file once to Immich and attach it to all target albums."""
        self.connect()
        file_path = Path(file_path).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"Photo file not found: {file_path}")

        stat = file_path.stat()
        created_at = (
            item.created_at
            if item and item.created_at
            else datetime.fromtimestamp(stat.st_mtime)
        )
        filename = item.filename if item and item.filename else file_path.name
        checksum = (
            item.checksum
            if item and item.checksum
            else hashlib.sha256(file_path.read_bytes()).hexdigest()
        )

        target_albums: list[str] = []
        if item:
            for a in item.albums:
                if a and a not in target_albums:
                    target_albums.append(a)
            if item.album and item.album not in target_albums:
                target_albums.append(item.album)
        if album and album not in target_albums:
            target_albums.append(album)

        data = {
            "deviceAssetId": f"crosspix-{filename}-{stat.st_size}-{int(stat.st_mtime)}",
            "deviceId": "crosspix-python",
            "fileCreatedAt": created_at.isoformat() + "Z",
            "fileModifiedAt": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z",
            "isFavorite": "false",
        }
        upload_headers = self._headers()
        sha1_checksum = (
            (item.raw_metadata.get("sha1_hex") or item.raw_metadata.get("sha1_b64"))
            if item
            else None
        )
        if isinstance(sha1_checksum, str) and sha1_checksum:
            upload_headers["x-immich-checksum"] = sha1_checksum

        with file_path.open("rb") as handle:
            files = {
                "assetData": (
                    filename,
                    handle,
                    item.mime_type if item else "image/jpeg",
                )
            }
            response = self.http_client.post(
                f"{self.base_url}/assets",
                headers=upload_headers,
                data=data,
                files=files,
            )
        response.raise_for_status()
        payload = response.json()
        asset_id = str(payload.get("id", ""))

        for alb_name in target_albums:
            alb_id = self._resolve_album_id(alb_name, create_if_missing=True)
            if alb_id:
                self._add_asset_to_album(asset_id, alb_id)

        return PhotoItem(
            id=asset_id,
            filename=filename,
            platform=self.platform_name,
            checksum=checksum,
            created_at=created_at,
            size=stat.st_size,
            mime_type=item.mime_type if item else "image/jpeg",
            album=target_albums[0] if target_albums else None,
            albums=target_albums,
            raw_metadata=payload,
        )

    def sync_albums(self, item: PhotoItem, albums: Iterable[str]) -> PhotoItem:
        """Attach an already-uploaded Immich asset to additional albums without re-uploading."""
        self.connect()
        for alb in albums:
            if alb and alb not in item.albums:
                alb_id = self._resolve_album_id(alb, create_if_missing=True)
                if alb_id:
                    self._add_asset_to_album(item.id, alb_id)
                item.add_album(alb)
        return item

    def close(self) -> None:
        self.http_client.close()
