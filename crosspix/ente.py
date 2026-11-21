"""Ente Photos provider using PyNaCl (libsodium E2EE), httpx, requests, and optional local export folder."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
import nacl.secret
import nacl.utils
import requests

from crosspix.base import PhotoItem, PhotoProvider
from crosspix.local import LinkMode, LocalProvider

DEFAULT_ENTE_API_URL = "https://api.ente.io"


class EnteProvider(PhotoProvider):
    """Photo provider for Ente Photos (E2EE Museum API via PyNaCl + httpx/requests or Ente export folder).

    Supports multi-album (multi-collection) deduplication and space-delimited folder hierarchies.
    """

    platform_name = "ente"

    def __init__(
        self,
        api_url: str = DEFAULT_ENTE_API_URL,
        auth_token: Optional[str] = None,
        master_key: Optional[bytes] = None,
        export_dir: Optional[str | Path] = None,
        http_client: Optional[httpx.Client] = None,
        album_delimiter: str = " ",
        link_mode: LinkMode = "hardlink",
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.auth_token = auth_token
        self.master_key = master_key or nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
        if len(self.master_key) != nacl.secret.SecretBox.KEY_SIZE:
            raise ValueError(
                f"Ente master_key must be exactly {nacl.secret.SecretBox.KEY_SIZE} bytes."
            )
        self.secret_box = nacl.secret.SecretBox(self.master_key)
        self.export_dir = Path(export_dir).expanduser().resolve() if export_dir else None
        self.http_client = http_client or httpx.Client(timeout=60.0)
        self.album_delimiter = album_delimiter
        self.link_mode: LinkMode = link_mode
        self._local_mirror = (
            LocalProvider(
                self.export_dir,
                recursive=True,
                album_delimiter=album_delimiter,
                link_mode=link_mode,
            )
            if self.export_dir
            else None
        )
        self._collection_cache: dict[str, int] = {}

    def connect(self) -> None:
        """Initialize connection to Ente API or local Ente CLI export directory."""
        if self._local_mirror is not None:
            self._local_mirror.connect()
            return
        if not self.auth_token:
            raise ValueError(
                "EnteProvider requires either an 'auth_token' for the Ente API "
                "or an 'export_dir' managed by ente-cli."
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.auth_token:
            headers["X-Auth-Token"] = self.auth_token
        return headers

    def encrypt_payload(self, plaintext: bytes) -> tuple[bytes, bytes]:
        """Encrypt bytes using PyNaCl SecretBox (XSalsa20-Poly1305) returning (ciphertext, nonce)."""
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
        encrypted = self.secret_box.encrypt(plaintext, nonce)
        return encrypted.ciphertext, nonce

    def decrypt_payload(self, ciphertext: bytes, nonce: bytes) -> bytes:
        """Decrypt bytes using PyNaCl SecretBox."""
        return self.secret_box.decrypt(ciphertext, nonce)

    def _parse_ente_file(
        self,
        raw: dict[str, Any],
        album: Optional[str] = None,
        albums: Optional[list[str]] = None,
    ) -> PhotoItem:
        metadata = raw.get("metadata", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}

        created_at: Optional[datetime] = None
        creation_usec = metadata.get("creationTime") or raw.get("creationTime")
        if isinstance(creation_usec, (int, float)):
            # Ente timestamps are stored in microseconds
            ts = creation_usec / 1_000_000 if creation_usec > 1e12 else creation_usec
            created_at = datetime.fromtimestamp(ts)

        filename = str(
            metadata.get("title")
            or raw.get("filename")
            or f"ente_{raw.get('id', 'photo')}.jpg"
        )

        all_albums = list(albums) if albums else []
        col_name = album or raw.get("collectionName")
        if col_name and col_name not in all_albums:
            all_albums.append(str(col_name))

        return PhotoItem(
            id=str(raw.get("id", "")),
            filename=filename,
            platform=self.platform_name,
            checksum=metadata.get("hash") or raw.get("hash"),
            created_at=created_at,
            size=raw.get("size"),
            mime_type=str(metadata.get("mimeType", "image/jpeg")),
            album=all_albums[0] if all_albums else None,
            albums=all_albums,
            raw_metadata=raw,
        )

    def _resolve_collection_id(
        self, collection_name: str, create_if_missing: bool = False
    ) -> Optional[int]:
        """Resolve an Ente collection (album) title to its integer ID, creating it if requested."""
        if collection_name in self._collection_cache:
            return self._collection_cache[collection_name]

        resp = self.http_client.get(
            f"{self.api_url}/collections/v2",
            headers=self._headers(),
        )
        resp.raise_for_status()
        for col in resp.json().get("collections", []):
            name = col.get("name")
            col_id = col.get("id")
            if name and col_id is not None:
                self._collection_cache[str(name)] = int(col_id)

        if collection_name in self._collection_cache:
            return self._collection_cache[collection_name]

        if create_if_missing:
            create_resp = self.http_client.post(
                f"{self.api_url}/collections",
                headers=self._headers(),
                json={"name": collection_name, "type": "album"},
            )
            create_resp.raise_for_status()
            col_obj = create_resp.json().get("collection", create_resp.json())
            col_id = col_obj.get("id")
            if col_id is not None:
                self._collection_cache[collection_name] = int(col_id)
                return int(col_id)

        return None

    def _add_file_to_collection(self, file_id: str | int, collection_id: int) -> None:
        """Link an existing Ente file into another collection without re-uploading bytes to S3."""
        resp = self.http_client.post(
            f"{self.api_url}/collections/add-files",
            headers=self._headers(),
            json={
                "collectionID": collection_id,
                "files": [{"id": int(file_id) if str(file_id).isdigit() else file_id}],
            },
        )
        resp.raise_for_status()

    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """Yield deduplicated PhotoItems from Ente export folder or Ente Museum API."""
        if self._local_mirror is not None:
            for item in self._local_mirror.list_photos(album=album):
                item.platform = self.platform_name
                yield item
            return

        resp = self.http_client.get(
            f"{self.api_url}/collections/v2",
            headers=self._headers(),
        )
        resp.raise_for_status()
        collections = resp.json().get("collections", [])

        dedup_items: dict[str, PhotoItem] = {}

        for col in collections:
            col_name = col.get("name")
            col_id = col.get("id")
            if col_name and col_id is not None:
                self._collection_cache[str(col_name)] = int(col_id)

            if album and col_name != album:
                continue

            files_resp = self.http_client.get(
                f"{self.api_url}/collections/v2/diff",
                params={"collectionID": col_id, "sinceTime": 0},
                headers=self._headers(),
            )
            files_resp.raise_for_status()
            for raw_file in files_resp.json().get("diff", []):
                if raw_file.get("isDeleted", False):
                    continue
                parsed = self._parse_ente_file(raw_file, album=col_name)
                dedup_key = parsed.checksum or f"{parsed.filename}:{parsed.created_at}:{parsed.id}"
                if dedup_key in dedup_items:
                    dedup_items[dedup_key].add_album(col_name)
                else:
                    dedup_items[dedup_key] = parsed

        yield from dedup_items.values()

    def download_photo(self, item: PhotoItem, destination_dir: Path) -> Path:
        """Download and decrypt a photo from Ente into destination_dir."""
        destination_dir = Path(destination_dir).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)

        if self._local_mirror is not None and item.local_path:
            return self._local_mirror.download_photo(item, destination_dir)

        download_url = item.raw_metadata.get("downloadUrl") or (
            f"{self.api_url}/files/download/{item.id}"
        )
        response = requests.get(download_url, headers=self._headers(), timeout=60)
        response.raise_for_status()
        payload = response.content

        nonce_b64 = item.raw_metadata.get("fileNonce")
        if nonce_b64:
            nonce = base64.b64decode(nonce_b64)
            payload = self.decrypt_payload(payload, nonce)

        target_path = destination_dir / item.filename
        target_path.write_bytes(payload)
        return target_path

    def upload_photo(
        self,
        file_path: Path,
        item: Optional[PhotoItem] = None,
        album: Optional[str] = None,
    ) -> PhotoItem:
        """Encrypt and upload a photo once to Ente (or export folder) and link it to all target albums."""
        file_path = Path(file_path).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"Photo file not found: {file_path}")

        if self._local_mirror is not None:
            uploaded = self._local_mirror.upload_photo(file_path, item=item, album=album)
            uploaded.platform = self.platform_name
            return uploaded

        target_albums: list[str] = []
        if item:
            for a in item.albums:
                if a and a not in target_albums:
                    target_albums.append(a)
            if item.album and item.album not in target_albums:
                target_albums.append(item.album)
        if album and album not in target_albums:
            target_albums.append(album)

        primary_album = target_albums[0] if target_albums else None
        primary_col_id = (
            self._resolve_collection_id(primary_album, create_if_missing=True)
            if primary_album
            else (item.raw_metadata.get("collectionID", 1) if item else 1)
        )

        raw_bytes = file_path.read_bytes()
        checksum = hashlib.sha256(raw_bytes).hexdigest()
        ciphertext, nonce = self.encrypt_payload(raw_bytes)
        filename = item.filename if item and item.filename else file_path.name
        created_at = (
            item.created_at
            if item and item.created_at
            else datetime.fromtimestamp(file_path.stat().st_mtime)
        )

        # Request pre-signed upload URL from Ente Museum server
        url_resp = self.http_client.get(
            f"{self.api_url}/files/upload-urls",
            params={"count": 1},
            headers=self._headers(),
        )
        url_resp.raise_for_status()
        upload_slot = url_resp.json()["urls"][0]

        # Upload encrypted blob directly to S3 pre-signed URL once via requests
        put_resp = requests.put(
            upload_slot["url"],
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120,
        )
        put_resp.raise_for_status()

        metadata_obj = {
            "title": filename,
            "creationTime": int(created_at.timestamp() * 1_000_000),
            "hash": checksum,
        }
        create_payload = {
            "collectionID": primary_col_id or 1,
            "file": {
                "objectKey": upload_slot["objectKey"],
                "decryptionHeader": base64.b64encode(nonce).decode("ascii"),
            },
            "metadata": metadata_obj,
        }
        commit_resp = self.http_client.post(
            f"{self.api_url}/files",
            headers=self._headers(),
            json=create_payload,
        )
        commit_resp.raise_for_status()
        created_data = commit_resp.json()
        created_data.setdefault("metadata", metadata_obj)
        created_data.setdefault("size", len(raw_bytes))
        created_item = self._parse_ente_file(created_data, albums=target_albums)

        # Link the uploaded file into any additional collections without re-uploading
        for extra_album in target_albums[1:]:
            extra_col_id = self._resolve_collection_id(extra_album, create_if_missing=True)
            if extra_col_id is not None:
                self._add_file_to_collection(created_item.id, extra_col_id)

        return created_item

    def sync_albums(self, item: PhotoItem, albums: Iterable[str]) -> PhotoItem:
        """Link an existing Ente photo into additional collections or local album folders."""
        if self._local_mirror is not None:
            updated = self._local_mirror.sync_albums(item, albums)
            updated.platform = self.platform_name
            return updated

        for alb in albums:
            if alb and alb not in item.albums:
                col_id = self._resolve_collection_id(alb, create_if_missing=True)
                if col_id is not None:
                    self._add_file_to_collection(item.id, col_id)
                item.add_album(alb)
        return item

    def close(self) -> None:
        self.http_client.close()
