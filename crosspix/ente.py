"""Ente Photos provider using PyNaCl (libsodium E2EE), httpx, requests, ente-cli selective download, or local export folder."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Iterable, Optional

import httpx
import nacl.secret
import nacl.utils
import requests

from crosspix.base import PhotoItem, PhotoProvider
from crosspix.local import LinkMode, LocalProvider

DEFAULT_ENTE_API_URL = "https://api.ente.io"
_ENTE_SYSTEM_ALBUMS = {"uncategorized", "trash", "corbeille", "archive", "hidden"}
_ENTE_TRASH_ALBUMS = {"trash", "corbeille"}


def _default_log(msg: str) -> None:
    print(msg, flush=True)


class EnteProvider(PhotoProvider):
    """Photo provider for Ente Photos.

    Supports three modes:
    1. Selective CLI mode (`ente-cli v0.4.5+`): Lists all albums and files as JSON via
       `ente photos album list --all --json` and `ente photos file list --all --json`,
       and downloads ONLY the photos missing from the target via `ente photos file download`.
    2. Local export directory mirror (`export_dir` without `run_cli_export`).
    3. Direct Ente Museum HTTP API (`auth_token` + PyNaCl E2EE).
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
        run_cli_export: bool = False,
        account: Optional[str] = None,
        on_progress: Optional[Callable[[str], None]] = _default_log,
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
        self.run_cli_export = run_cli_export
        self.account = account
        self.on_progress = on_progress

        # Use local mirror only when export_dir is specified without run_cli_export
        self._local_mirror = (
            LocalProvider(
                self.export_dir,
                recursive=True,
                album_delimiter=album_delimiter,
                link_mode=link_mode,
                on_progress=on_progress,
            )
            if (self.export_dir and not self.run_cli_export)
            else None
        )
        self._use_cli: bool = False
        self._ente_bin: Optional[str] = None
        self._collection_cache: dict[str, int] = {}

    def _log(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    @staticmethod
    def _find_ente_binary() -> Optional[str]:
        ente_bin = shutil.which("ente")
        if ente_bin:
            return ente_bin
        fallback = Path.home() / ".local" / "bin" / "ente"
        if fallback.exists():
            return str(fallback)
        return None

    def connect(self) -> None:
        """Initialize connection to Ente CLI, local export folder, or Ente Museum API."""
        if self._local_mirror is not None:
            self._local_mirror.connect()
            self._log(f"[Ente] Connected to Ente export directory: {self.export_dir}")
            return

        if self.auth_token:
            self._log(f"[Ente] Connected to Ente API ({self.api_url}).")
            return

        ente_bin = self._find_ente_binary()
        if ente_bin or self.run_cli_export:
            if not ente_bin:
                raise RuntimeError(
                    "The 'ente' CLI binary was not found in PATH or ~/.local/bin/ente."
                )
            self._ente_bin = ente_bin
            self._use_cli = True

            acct_check = subprocess.run(
                [ente_bin, "account", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
            if "No accounts on this device" in (acct_check.stdout + acct_check.stderr):
                self._log(
                    "[Ente] No account logged in to ente-cli yet. Launching 'ente account login --app photos'..."
                )
                login_cmd = [ente_bin, "account", "login", "--app", "photos"]
                if self.account:
                    login_cmd.extend(["--account", self.account])
                subprocess.run(login_cmd, check=True)

            self._log(f"[Ente] Connected via ente-cli ({ente_bin}).")
            return

        raise ValueError(
            "EnteProvider requires either ente-cli installed, an 'auth_token' for the Ente API, "
            "or an 'export_dir'."
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

    def _list_via_cli(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """List all albums and files via ente-cli JSON commands without bulk-exporting files."""
        assert self._ente_bin is not None
        acct_args = ["--account", self.account] if self.account else []

        self._log("[Ente] Fetching album list via 'ente photos album list --all --json'...")
        alb_cmd = [self._ente_bin, "photos", "album", "list", "--all", "--json", *acct_args]
        alb_proc = subprocess.run(alb_cmd, capture_output=True, text=True, check=True)
        raw_albums = json.loads(alb_proc.stdout)

        album_id_to_name: dict[str, str] = {}
        trash_album_ids: set[str] = set()
        for alb in raw_albums:
            if not isinstance(alb, dict):
                continue
            aid = str(alb.get("id", ""))
            name = str(alb.get("name", "")).strip()
            alb_type = str(alb.get("type", "")).lower()
            if not aid or not name:
                continue
            if name.lower() in _ENTE_TRASH_ALBUMS or alb_type == "trash":
                trash_album_ids.add(aid)
                continue
            if name.lower() in _ENTE_SYSTEM_ALBUMS or alb_type in {"uncategorized", "defaulthidden"}:
                continue
            album_id_to_name[aid] = name

        self._log(
            f"[Ente] Found {len(album_id_to_name)} user album(s). Fetching file metadata list..."
        )
        file_cmd = [
            self._ente_bin,
            "photos",
            "file",
            "list",
            "--all",
            "--json",
            "--offline",
            *acct_args,
        ]
        if album:
            file_cmd.extend(["--album", album])

        file_proc = subprocess.run(file_cmd, capture_output=True, text=True, check=False)
        if file_proc.returncode != 0:
            # Retry without --offline if local metadata cache was not populated yet
            file_cmd_online = [arg for arg in file_cmd if arg != "--offline"]
            file_proc = subprocess.run(
                file_cmd_online, capture_output=True, text=True, check=True
            )

        raw_files = json.loads(file_proc.stdout)
        self._log(f"[Ente] Loaded metadata for {len(raw_files)} file entry/entries from Ente.")

        dedup_items: dict[str, PhotoItem] = {}
        for raw in raw_files:
            if not isinstance(raw, dict):
                continue
            file_id = str(raw.get("id", ""))
            filename = str(raw.get("name") or f"ente_{file_id}.jpg")
            alb_ids = [str(a) for a in raw.get("albumIds", []) if a is not None]

            # Skip files that only exist in Trash/Corbeille
            if alb_ids and all(aid in trash_album_ids for aid in alb_ids):
                continue

            item_albums = [
                album_id_to_name[aid]
                for aid in alb_ids
                if aid in album_id_to_name
            ]
            if album and album not in item_albums:
                item_albums.append(album)

            created_at: Optional[datetime] = None
            dt_str = raw.get("dateTime") or raw.get("createdAt")
            if isinstance(dt_str, str):
                try:
                    created_at = datetime.fromisoformat(
                        dt_str.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    pass

            hash_val = raw.get("hash")
            if isinstance(hash_val, str) and ":" in hash_val:
                # For Live Photos, Ente stores "image_hash:video_hash"
                hash_val = hash_val.split(":", 1)[0]

            mime_type, _ = mimetypes.guess_type(filename)
            raw_meta = {**raw}
            if isinstance(hash_val, str) and hash_val:
                raw_meta["sha512_b64"] = hash_val

            dedup_key = (
                hash_val
                if isinstance(hash_val, str) and hash_val
                else f"{filename}:{raw.get('sizeBytes')}:{created_at}:{file_id}"
            )
            if dedup_key in dedup_items:
                for alb_name in item_albums:
                    dedup_items[dedup_key].add_album(alb_name)
            else:
                dedup_items[dedup_key] = PhotoItem(
                    id=file_id,
                    filename=filename,
                    platform=self.platform_name,
                    checksum=hash_val if isinstance(hash_val, str) else None,
                    created_at=created_at,
                    size=raw.get("sizeBytes"),
                    width=raw.get("width"),
                    height=raw.get("height"),
                    mime_type=mime_type or "image/jpeg",
                    album=item_albums[0] if item_albums else None,
                    albums=item_albums,
                    raw_metadata=raw_meta,
                )

        yield from dedup_items.values()

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
        """Yield deduplicated PhotoItems from Ente CLI, local export folder, or Ente Museum API."""
        if self._local_mirror is not None:
            self._log(f"[Ente] Scanning Ente export folder '{self.export_dir}'...")
            for item in self._local_mirror.list_photos(album=album):
                item.platform = self.platform_name
                item.albums = [
                    a for a in item.albums if a.lower() not in _ENTE_SYSTEM_ALBUMS
                ]
                item.album = item.albums[0] if item.albums else None
                yield item
            return

        if self._use_cli:
            yield from self._list_via_cli(album=album)
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
        """Download and decrypt a single photo from Ente into destination_dir."""
        destination_dir = Path(destination_dir).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)

        if self._local_mirror is not None and item.local_path:
            return self._local_mirror.download_photo(item, destination_dir)

        if self._use_cli and self._ente_bin:
            target_path = destination_dir / item.filename
            if target_path.exists():
                target_path.unlink()
            acct_args = ["--account", self.account] if self.account else []
            dl_cmd = [
                self._ente_bin,
                "photos",
                "file",
                "download",
                "--output",
                str(target_path),
                *acct_args,
                str(item.id),
            ]
            subprocess.run(dl_cmd, check=True)
            return target_path

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
