"""Google Photos provider using gphotospy, google-api-python-client, google-auth-oauthlib, and requests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import pickle
import re
import shutil
import time
from typing import Any, Callable, Iterable, Optional
import webbrowser

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from gphotospy import authorize
from gphotospy.album import Album
from gphotospy.media import Media
import requests

from crosspix.base import PhotoItem, PhotoProvider
from crosspix.local import LocalProvider, SUPPORTED_EXTENSIONS

GOOGLE_PHOTOS_UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
GOOGLE_PHOTOS_BATCH_CREATE_URL = (
    "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
)
GOOGLE_PHOTOS_ALBUMS_URL = "https://photoslibrary.googleapis.com/v1/albums"
GOOGLE_PHOTOS_PICKER_SESSIONS_URL = "https://photospicker.googleapis.com/v1/sessions"
GOOGLE_PHOTOS_PICKER_MEDIA_URL = "https://photospicker.googleapis.com/v1/mediaItems"

GOOGLE_PHOTOS_SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",
    "https://www.googleapis.com/auth/photoslibrary.sharing",
]

GOOGLE_PHOTOS_MODERN_SCOPES = [
    "https://www.googleapis.com/auth/photospicker.mediaitems.readonly",
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
]


def _default_log(msg: str) -> None:
    print(msg, flush=True)


_TAKEOUT_SYSTEM_FOLDER_RE = re.compile(
    r"^(Photos from \d{4}|Photos de \d{4}|Trash|Corbeille|Archive)$",
    re.IGNORECASE,
)


class GooglePhotosProvider(PhotoProvider):
    """Photo provider for synchronizing photos and multi-album memberships with Google Photos."""

    platform_name = "google_photos"

    def __init__(
        self,
        client_secret_file: str | Path = "gphoto_oauth.json",
        access_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        include_albums: bool = True,
        takeout_dir: Optional[str | Path] = None,
        on_progress: Optional[Callable[[str], None]] = _default_log,
    ) -> None:
        self.client_secret_file = Path(client_secret_file).expanduser()
        self.access_token = access_token
        self.session = session or requests.Session()
        self.include_albums = include_albums
        self.takeout_dir = (
            Path(takeout_dir).expanduser().resolve() if takeout_dir else None
        )
        self.on_progress = on_progress
        self.service: Any = None
        self.credentials: Any = None
        self.album_manager: Optional[Album] = None
        self.media_manager: Optional[Media] = None
        self._album_cache: dict[str, str] = {}

    def _log(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    def connect(self) -> None:
        """Authenticate with Google Photos using gphotospy, OAuth token, or local Google Takeout folder."""
        if self.takeout_dir is not None:
            if not self.takeout_dir.exists():
                raise FileNotFoundError(
                    f"Google Takeout directory not found: {self.takeout_dir}"
                )
            self._log(
                f"[Google Photos] Connected to local Google Takeout export: {self.takeout_dir}"
            )
            return

        if self.service is not None or self.access_token is not None:
            return

        if self.client_secret_file.exists():
            self._log(
                f"[Google Photos] Authenticating using '{self.client_secret_file}'..."
            )
            self.credentials = authorize.get_credentials(str(self.client_secret_file))
            if self.credentials and getattr(self.credentials, "token", None):
                self.access_token = self.credentials.token
            self.service = authorize.init(str(self.client_secret_file))
            if self.service is not None:
                self.album_manager = Album(self.service)
                self.media_manager = Media(self.service)
                self._log("[Google Photos] Connected to Google Photos API.")
        else:
            raise FileNotFoundError(
                f"Google OAuth credentials file '{self.client_secret_file}' not found "
                "and no access_token was provided."
            )

    def authenticate_with_oauth_flow(
        self,
        port: int = 0,
        scopes: Optional[list[str]] = None,
        token_filename: str = "google_photos_modern.token",
    ) -> Any:
        """Run an explicit InstalledAppFlow via google-auth-oauthlib and cache credentials."""
        target_scopes = scopes or GOOGLE_PHOTOS_MODERN_SCOPES
        token_path = self.client_secret_file.resolve().parent / token_filename
        creds = None

        if token_path.exists():
            try:
                with token_path.open("rb") as fh:
                    creds = pickle.load(fh)
            except Exception:
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                self._log("[Google Photos] Refreshing OAuth token...")
                creds.refresh(Request())
            else:
                self._log(
                    "[Google Photos] Opening browser for Google Photos Picker OAuth authorization..."
                )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.client_secret_file), scopes=target_scopes
                )
                creds = flow.run_local_server(port=port)
            with token_path.open("wb") as fh:
                pickle.dump(creds, fh)

        self.credentials = creds
        self.access_token = creds.token
        self._log("[Google Photos] OAuth token obtained.")
        return creds

    def _auth_headers(self) -> dict[str, str]:
        if self.credentials and getattr(self.credentials, "expired", False):
            if getattr(self.credentials, "refresh_token", None):
                self.credentials.refresh(Request())
                self.access_token = self.credentials.token
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        return {}

    @staticmethod
    def _parse_media_item(
        raw: dict[str, Any],
        album: Optional[str] = None,
        albums: Optional[list[str]] = None,
    ) -> PhotoItem:
        # Support both Library API (mediaMetadata) and Picker API (mediaFile) formats
        media_file = raw.get("mediaFile", {})
        meta = raw.get("mediaMetadata") or media_file.get("mediaFileMetadata") or {}

        created_at: Optional[datetime] = None
        creation_time = meta.get("creationTime") or raw.get("createTime")
        if isinstance(creation_time, str):
            try:
                created_at = datetime.fromisoformat(
                    creation_time.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except ValueError:
                pass

        width = (
            int(meta["width"])
            if "width" in meta and str(meta["width"]).isdigit()
            else None
        )
        height = (
            int(meta["height"])
            if "height" in meta and str(meta["height"]).isdigit()
            else None
        )

        filename = str(
            raw.get("filename")
            or media_file.get("filename")
            or f"{raw.get('id', 'photo')}.jpg"
        )
        mime_type = str(
            raw.get("mimeType")
            or media_file.get("mimeType")
            or "image/jpeg"
        )
        base_url = raw.get("baseUrl") or media_file.get("baseUrl")
        if base_url and "baseUrl" not in raw:
            raw = {**raw, "baseUrl": base_url}

        all_albums = list(albums) if albums else []
        if album and album not in all_albums:
            all_albums.append(album)

        return PhotoItem(
            id=str(raw.get("id", "")),
            filename=filename,
            platform="google_photos",
            created_at=created_at,
            width=width,
            height=height,
            mime_type=mime_type,
            album=all_albums[0] if all_albums else album,
            albums=all_albums,
            raw_metadata=raw,
        )

    def _resolve_album_id(self, album_title: str, create_if_missing: bool = False) -> Optional[str]:
        if album_title in self._album_cache:
            return self._album_cache[album_title]

        if self.album_manager is not None:
            for alb in self.album_manager.list() or []:
                title = alb.get("title")
                alb_id = alb.get("id")
                if title and alb_id:
                    self._album_cache[str(title)] = str(alb_id)
            if album_title in self._album_cache:
                return self._album_cache[album_title]
            if create_if_missing:
                self._log(f"[Google Photos] Creating album '{album_title}'...")
                created = self.album_manager.create(album_title)
                if created and "id" in created:
                    self._album_cache[album_title] = str(created["id"])
                    return self._album_cache[album_title]
        elif self.access_token and create_if_missing:
            self._log(f"[Google Photos] Creating album '{album_title}'...")
            resp = self.session.post(
                GOOGLE_PHOTOS_ALBUMS_URL,
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={"album": {"title": album_title}},
                timeout=30,
            )
            resp.raise_for_status()
            alb_id = resp.json().get("id")
            if alb_id:
                self._album_cache[album_title] = str(alb_id)
                return str(alb_id)
        return None

    def _add_media_to_album(self, media_id: str, album_id: str) -> None:
        """Attach an existing Google Photos mediaItemId to an album via batchAddMediaItems."""
        if not media_id or not album_id:
            return
        resp = self.session.post(
            f"{GOOGLE_PHOTOS_ALBUMS_URL}/{album_id}:batchAddMediaItems",
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json={"mediaItemIds": [media_id]},
            timeout=30,
        )
        resp.raise_for_status()

    def _list_via_picker_api(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """Interactive fallback using the Google Photos Picker API (post-March 2025 scopes)."""
        self._log(
            "[Google Photos] Switching to Google Photos Picker API (photospicker.googleapis.com)..."
        )
        self.authenticate_with_oauth_flow(scopes=GOOGLE_PHOTOS_MODERN_SCOPES)

        self._log("[Google Photos] Creating Picker session...")
        sess_resp = self.session.post(
            GOOGLE_PHOTOS_PICKER_SESSIONS_URL,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json={},
            timeout=30,
        )
        if sess_resp.status_code != 200:
            self._log(
                f"[Google Photos] Picker API error ({sess_resp.status_code}): {sess_resp.text}"
            )
        sess_resp.raise_for_status()
        session_data = sess_resp.json()
        session_id = session_data["id"]
        picker_uri = session_data["pickerUri"]

        self._log(
            f"\n[Google Photos] Please select the photos to synchronize in your browser:\n"
            f"  -> {picker_uri}\n"
            f"Waiting for you to click 'Done' in Google Photos Picker..."
        )
        try:
            webbrowser.open(picker_uri)
        except Exception:
            pass

        waited = 0
        while True:
            time.sleep(3)
            waited += 3
            poll_resp = self.session.get(
                f"{GOOGLE_PHOTOS_PICKER_SESSIONS_URL}/{session_id}",
                headers=self._auth_headers(),
                timeout=30,
            )
            poll_resp.raise_for_status()
            if poll_resp.json().get("mediaItemsSet"):
                self._log("[Google Photos] Photo selection confirmed! Fetching selected items...")
                break
            if waited % 15 == 0:
                self._log(
                    f"[Google Photos] Still waiting for selection in browser ({waited}s elapsed)..."
                )

        page_token: Optional[str] = None
        count = 0
        while True:
            params: dict[str, Any] = {"sessionId": session_id, "pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            items_resp = self.session.get(
                GOOGLE_PHOTOS_PICKER_MEDIA_URL,
                headers=self._auth_headers(),
                params=params,
                timeout=60,
            )
            items_resp.raise_for_status()
            payload = items_resp.json()
            for raw in payload.get("mediaItems", []):
                count += 1
                item = self._parse_media_item(raw, album=album)
                self._log(f"[Google Photos] Found selected photo #{count}: {item.filename}")
                yield item
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

    def _list_from_takeout(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """Scan a Google Takeout directory, deduplicating photos across albums by SHA-256."""
        assert self.takeout_dir is not None
        self._log(f"[Google Photos] Scanning Google Takeout directory '{self.takeout_dir}'...")
        items_by_checksum: dict[str, PhotoItem] = {}

        for path in sorted(self.takeout_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            folder_name = path.parent.name.strip()
            is_system_folder = bool(
                _TAKEOUT_SYSTEM_FOLDER_RE.match(folder_name)
                or path.parent == self.takeout_dir
                or folder_name.lower() in {"google photos", "google\u00a0photos"}
            )
            alb_name = None if is_system_folder else folder_name

            checksum = LocalProvider.compute_sha256(path)
            if checksum in items_by_checksum:
                if alb_name:
                    items_by_checksum[checksum].add_album(alb_name)
                continue

            width, height, created_at, exif_meta = LocalProvider.extract_image_metadata(
                path
            )
            item = PhotoItem(
                id=checksum,
                filename=path.name,
                platform=self.platform_name,
                checksum=checksum,
                created_at=created_at,
                size=path.stat().st_size,
                width=width,
                height=height,
                album=alb_name,
                albums=[alb_name] if alb_name else [],
                local_path=path,
                raw_metadata=exif_meta,
            )
            items_by_checksum[checksum] = item

        for item in items_by_checksum.values():
            if album and album not in item.albums:
                continue
            yield item

    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """Yield deduplicated PhotoItem objects from Google Photos with all their album memberships."""
        if self.takeout_dir is not None:
            yield from self._list_from_takeout(album=album)
            return

        if self.media_manager is None and self.access_token is None:
            self.connect()

        if self.media_manager is None:
            return

        try:
            if album:
                self._log(f"[Google Photos] Searching for album '{album}'...")
                album_id = self._resolve_album_id(album, create_if_missing=False)
                if not album_id:
                    self._log(f"[Google Photos] Album '{album}' not found.")
                    return
                for raw in self.media_manager.search_album(album_id) or []:
                    if isinstance(raw, dict):
                        yield self._parse_media_item(raw, album=album)
                return

            items_by_id: dict[str, PhotoItem] = {}

            # 1. Load main library media items
            self._log("[Google Photos] Listing media items from library...")
            for raw in self.media_manager.list() or []:
                if isinstance(raw, dict) and raw.get("id"):
                    item = self._parse_media_item(raw)
                    items_by_id[item.id] = item
                    if len(items_by_id) % 50 == 0:
                        self._log(
                            f"[Google Photos] Scanned {len(items_by_id)} library items so far..."
                        )
            self._log(f"[Google Photos] Found {len(items_by_id)} items in main library.")

            # 2. Enrich items with all their Google Photos album memberships
            if self.include_albums and self.album_manager is not None:
                self._log("[Google Photos] Listing albums and resolving memberships...")
                for alb in self.album_manager.list() or []:
                    if not isinstance(alb, dict):
                        continue
                    alb_title = alb.get("title")
                    alb_id = alb.get("id")
                    if not alb_title or not alb_id:
                        continue
                    self._log(f"[Google Photos] Scanning album '{alb_title}'...")
                    self._album_cache[str(alb_title)] = str(alb_id)
                    for raw in self.media_manager.search_album(alb_id) or []:
                        if not isinstance(raw, dict) or not raw.get("id"):
                            continue
                        media_id = str(raw["id"])
                        if media_id in items_by_id:
                            items_by_id[media_id].add_album(str(alb_title))
                        else:
                            items_by_id[media_id] = self._parse_media_item(
                                raw, album=str(alb_title)
                            )

            yield from items_by_id.values()
        except HttpError as err:
            reason = getattr(err, "_get_reason", lambda: str(err))()
            self._log(f"[Google Photos] Library API returned HTTP {err.resp.status}: {reason}")
            if getattr(err, "resp", None) is not None and err.resp.status == 403:
                yield from self._list_via_picker_api(album=album)
            else:
                raise

    def download_photo(self, item: PhotoItem, destination_dir: Path) -> Path:
        """Download full-resolution media from Google Photos using its baseUrl."""
        destination_dir = Path(destination_dir).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)

        base_url = item.raw_metadata.get("baseUrl") or item.raw_metadata.get(
            "mediaFile", {}
        ).get("baseUrl")
        if not base_url and self.media_manager is not None:
            fresh = self.media_manager.get(item.id)
            base_url = fresh.get("baseUrl") if isinstance(fresh, dict) else None

        if not base_url:
            raise ValueError(f"Google Photos item '{item.id}' does not contain a baseUrl.")

        is_video = "video" in (item.mime_type or "").lower()
        download_url = f"{base_url}=dv" if is_video else f"{base_url}=d"

        self._log(f"[Google Photos] Downloading '{item.filename}'...")
        response = self.session.get(download_url, headers=self._auth_headers(), timeout=60)
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
        """Upload a local image once to Google Photos and assign it to all target albums."""
        file_path = Path(file_path).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"Photo file not found: {file_path}")

        filename = item.filename if item and item.filename else file_path.name
        mime_type = item.mime_type if item and item.mime_type else "application/octet-stream"

        self._log(f"[Google Photos] Uploading '{filename}'...")
        upload_headers = {
            **self._auth_headers(),
            "Content-type": "application/octet-stream",
            "X-Goog-Upload-Content-Type": mime_type,
            "X-Goog-Upload-Protocol": "raw",
            "X-Goog-Upload-File-Name": filename,
        }
        with file_path.open("rb") as fh:
            upload_resp = self.session.post(
                GOOGLE_PHOTOS_UPLOAD_URL,
                headers=upload_headers,
                data=fh.read(),
                timeout=120,
            )
        upload_resp.raise_for_status()
        upload_token = upload_resp.text.strip()

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
        primary_album_id = (
            self._resolve_album_id(primary_album, create_if_missing=True)
            if primary_album
            else None
        )

        payload: dict[str, Any] = {
            "newMediaItems": [
                {
                    "description": f"Synced by crosspix ({filename})",
                    "simpleMediaItem": {
                        "fileName": filename,
                        "uploadToken": upload_token,
                    },
                }
            ]
        }
        if primary_album_id:
            payload["albumId"] = primary_album_id

        create_resp = self.session.post(
            GOOGLE_PHOTOS_BATCH_CREATE_URL,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        create_resp.raise_for_status()
        data = create_resp.json()
        results = data.get("newMediaItemResults", [{}])
        media_item = results[0].get("mediaItem", {"id": upload_token, "filename": filename})
        created_item = self._parse_media_item(media_item, albums=target_albums)

        # Attach the single uploaded mediaItemId to any remaining albums
        for extra_album in target_albums[1:]:
            extra_album_id = self._resolve_album_id(extra_album, create_if_missing=True)
            if extra_album_id:
                self._log(
                    f"[Google Photos] Linking '{filename}' to album '{extra_album}'..."
                )
                self._add_media_to_album(created_item.id, extra_album_id)

        return created_item

    def sync_albums(self, item: PhotoItem, albums: Iterable[str]) -> PhotoItem:
        """Attach an already-uploaded Google Photos item to additional albums."""
        for alb in albums:
            if alb and alb not in item.albums:
                alb_id = self._resolve_album_id(alb, create_if_missing=True)
                if alb_id:
                    self._log(
                        f"[Google Photos] Linking existing item '{item.filename}' to album '{alb}'..."
                    )
                    self._add_media_to_album(item.id, alb_id)
                item.add_album(alb)
        return item
