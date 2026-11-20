"""Immich provider using immichpy and httpx."""

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
    """Photo provider for synchronizing photos with an Immich server via immichpy."""

    platform_name = "immich"

    def __init__(
        self,
        base_url: str = "http://localhost:2283/api",
        api_key: str = "",
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http_client = http_client or httpx.Client(timeout=60.0)

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
    def _asset_to_photo_item(asset: Any, album: Optional[str] = None) -> PhotoItem:
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

        return PhotoItem(
            id=asset_id,
            filename=filename,
            platform="immich",
            checksum=checksum,
            created_at=created_at,
            mime_type=mime_type,
            album=album,
            raw_metadata=raw_meta,
        )

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
                    if alb_name == album and alb_id:
                        album_info = await client.album.get_album_info(id=alb_id)
                        assets = getattr(album_info, "assets", []) or []
                        return [self._asset_to_photo_item(a, album=album) for a in assets]
                return []

            query = MetadataSearchDto(size=250)
            results = await client.search.search_assets(body=query)
            assetsObj = getattr(results, "assets", results)
            items = getattr(assetsObj, "items", assetsObj)
            if isinstance(items, list):
                return [self._asset_to_photo_item(a) for a in items]
            return []

    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """List PhotoItems from Immich using immichpy AsyncClient."""
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
        """Upload a local photo file to Immich and return its PhotoItem."""
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

        data = {
            "deviceAssetId": f"crosspix-{filename}-{int(stat.st_mtime)}",
            "deviceId": "crosspix-python",
            "fileCreatedAt": created_at.isoformat() + "Z",
            "fileModifiedAt": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z",
            "isFavorite": "false",
        }
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
                headers=self._headers(),
                data=data,
                files=files,
            )
        response.raise_for_status()
        payload = response.json()

        return PhotoItem(
            id=str(payload.get("id", "")),
            filename=filename,
            platform=self.platform_name,
            checksum=checksum,
            created_at=created_at,
            size=stat.st_size,
            mime_type=item.mime_type if item else "image/jpeg",
            album=album or (item.album if item else None),
            raw_metadata=payload,
        )

    def close(self) -> None:
        self.http_client.close()
