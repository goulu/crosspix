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
GOOGLE_PHOTOS_SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",
    "https://www.googleapis.com/auth/photoslibrary.sharing",
]


class GooglePhotosProvider(PhotoProvider):
    """Photo provider for synchronizing photos with Google Photos."""

    platform_name = "google_photos"

    def __init__(
        self,
        client_secret_file: str | Path = "gphoto_oauth.json",
        access_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.client_secret_file = Path(client_secret_file).expanduser()
        self.access_token = access_token
        self.session = session or requests.Session()
        self.service: Any = None
        self.album_manager: Optional[Album] = None
        self.media_manager: Optional[Media] = None

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
    def _parse_media_item(raw: dict[str, Any], album: Optional[str] = None) -> PhotoItem:
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

        return PhotoItem(
            id=str(raw.get("id", "")),
            filename=str(raw.get("filename", f"{raw.get('id', 'photo')}.jpg")),
            platform="google_photos",
            created_at=created_at,
            width=width,
            height=height,
            mime_type=str(raw.get("mimeType", "image/jpeg")),
            album=album,
            raw_metadata=raw,
        )

    def _resolve_album_id(self, album_title: str, create_if_missing: bool = False) -> Optional[str]:
        if self.album_manager is not None:
            for alb in self.album_manager.list() or []:
                if alb.get("title") == album_title:
                    return str(alb.get("id"))
            if create_if_missing:
                created = self.album_manager.create(album_title)
                if created and "id" in created:
                    return str(created["id"])
        return None

    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """Yield PhotoItem objects from Google Photos."""
        if self.media_manager is None:
            self.connect()

        if self.media_manager is not None:
            if album:
                album_id = self._resolve_album_id(album, create_if_missing=False)
                if not album_id:
                    return
                iterator = self.media_manager.search_album(album_id)
            else:
                iterator = self.media_manager.list()

            for raw in iterator or []:
                if isinstance(raw, dict):
                    yield self._parse_media_item(raw, album=album)

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
        """Upload a local image to Google Photos and commit it via batchCreate."""
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

        target_album = album or (item.album if item else None)
        album_id = (
            self._resolve_album_id(target_album, create_if_missing=True)
            if target_album
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
        if album_id:
            payload["albumId"] = album_id

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
        return self._parse_media_item(media_item, album=target_album)
