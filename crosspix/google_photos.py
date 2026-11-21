"""Google Photos provider using gphotospy, google-api-python-client, google-auth-oauthlib, and requests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from gphotospy import authorize
from gphotospy.album import Album
from gphotospy.media import Media
import requests

from crosspix.base import PhotoItem, PhotoProvider

GOOGLE_PHOTOS_UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
GOOGLE_PHOTOS_BATCH_CREATE_URL = (
    "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
)
GOOGLE_PHOTOS_ALBUMS_URL = "https://photoslibrary.googleapis.com/v1/albums"
GOOGLE_PHOTOS_SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",
    "https://www.googleapis.com/auth/photoslibrary.sharing",
]


class GooglePhotosProvider(PhotoProvider):
    """Photo provider for synchronizing photos and multi-album memberships with Google Photos."""

    platform_name = "google_photos"

    def __init__(
        self,
        client_secret_file: str | Path = "gphoto_oauth.json",
        access_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        include_albums: bool = True,
    ) -> None:
        self.client_secret_file = Path(client_secret_file).expanduser()
        self.access_token = access_token
        self.session = session or requests.Session()
        self.include_albums = include_albums
        self.service: Any = None
        self.album_manager: Optional[Album] = None
        self.media_manager: Optional[Media] = None
        self._album_cache: dict[str, str] = {}

    def connect(self) -> None:
        """Authenticate with Google Photos using gphotospy or an OAuth access token."""
        if self.service is not None:
            return

        if self.client_secret_file.exists():
            self.service = authorize.init(str(self.client_secret_file))
            self.album_manager = Album(self.service)
            self.media_manager = Media(self.service)
        elif not self.access_token:
            raise FileNotFoundError(
                f"Google OAuth credentials file '{self.client_secret_file}' not found "
                "and no access_token was provided."
            )

    def authenticate_with_oauth_flow(self, port: int = 0) -> Any:
        """Run an explicit InstalledAppFlow via google-auth-oauthlib and build discovery service."""
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.client_secret_file), scopes=GOOGLE_PHOTOS_SCOPES
        )
        creds = flow.run_local_server(port=port)
        self.access_token = creds.token
        self.service = build(
            "photoslibrary", "v1", credentials=creds, static_discovery=False
        )
        return self.service

    def _auth_headers(self) -> dict[str, str]:
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        return {}

    @staticmethod
    def _parse_media_item(
        raw: dict[str, Any],
        album: Optional[str] = None,
        albums: Optional[list[str]] = None,
    ) -> PhotoItem:
        meta = raw.get("mediaMetadata", {})
        created_at: Optional[datetime] = None
        creation_time = meta.get("creationTime")
        if isinstance(creation_time, str):
            try:
                created_at = datetime.fromisoformat(creation_time.replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
            except ValueError:
                pass

        width = int(meta["width"]) if "width" in meta and str(meta["width"]).isdigit() else None
        height = (
            int(meta["height"]) if "height" in meta and str(meta["height"]).isdigit() else None
        )

        all_albums = list(albums) if albums else []
        if album and album not in all_albums:
            all_albums.append(album)

        return PhotoItem(
            id=str(raw.get("id", "")),
            filename=str(raw.get("filename", f"{raw.get('id', 'photo')}.jpg")),
            platform="google_photos",
            created_at=created_at,
            width=width,
            height=height,
            mime_type=str(raw.get("mimeType", "image/jpeg")),
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
                created = self.album_manager.create(album_title)
                if created and "id" in created:
                    self._album_cache[album_title] = str(created["id"])
                    return self._album_cache[album_title]
        elif self.access_token and create_if_missing:
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

    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """Yield deduplicated PhotoItem objects from Google Photos with all their album memberships."""
        if self.media_manager is None:
            self.connect()

        if self.media_manager is None:
            return

        if album:
            album_id = self._resolve_album_id(album, create_if_missing=False)
            if not album_id:
                return
            for raw in self.media_manager.search_album(album_id) or []:
                if isinstance(raw, dict):
                    yield self._parse_media_item(raw, album=album)
            return

        items_by_id: dict[str, PhotoItem] = {}

        # 1. Load main library media items
        for raw in self.media_manager.list() or []:
            if isinstance(raw, dict) and raw.get("id"):
                item = self._parse_media_item(raw)
                items_by_id[item.id] = item

        # 2. Enrich items with all their Google Photos album memberships
        if self.include_albums and self.album_manager is not None:
            for alb in self.album_manager.list() or []:
                if not isinstance(alb, dict):
                    continue
                alb_title = alb.get("title")
                alb_id = alb.get("id")
                if not alb_title or not alb_id:
                    continue
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

    def download_photo(self, item: PhotoItem, destination_dir: Path) -> Path:
        """Download full-resolution media from Google Photos using its baseUrl."""
        destination_dir = Path(destination_dir).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)

        base_url = item.raw_metadata.get("baseUrl")
        if not base_url and self.media_manager is not None:
            fresh = self.media_manager.get(item.id)
            base_url = fresh.get("baseUrl") if isinstance(fresh, dict) else None

        if not base_url:
            raise ValueError(f"Google Photos item '{item.id}' does not contain a baseUrl.")

        is_video = "video" in (item.mime_type or "").lower()
        download_url = f"{base_url}=dv" if is_video else f"{base_url}=d"

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
                self._add_media_to_album(created_item.id, extra_album_id)

        return created_item

    def sync_albums(self, item: PhotoItem, albums: Iterable[str]) -> PhotoItem:
        """Attach an already-uploaded Google Photos item to additional albums."""
        for alb in albums:
            if alb and alb not in item.albums:
                alb_id = self._resolve_album_id(alb, create_if_missing=True)
                if alb_id:
                    self._add_media_to_album(item.id, alb_id)
                item.add_album(alb)
        return item
