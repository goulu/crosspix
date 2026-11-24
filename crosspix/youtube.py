"""YouTube Data API v3 provider for uploading videos and organizing them into playlists."""

from __future__ import annotations

from datetime import datetime
import mimetypes
from pathlib import Path
import pickle
import re
import ssl
import time
from typing import Any, Callable, Iterable, Literal, Optional

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from crosspix.base import PhotoItem, PhotoProvider, is_video_filename

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
]

PrivacyStatus = Literal["private", "unlisted", "public"]

_SHA1_TAG_RE = re.compile(r"^crosspix_sha1:\s*([0-9a-f]{40})\s*$", re.IGNORECASE | re.MULTILINE)
_FILENAME_TAG_RE = re.compile(r"^Original filename:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def _sanitize_youtube_title(raw_title: str) -> str:
    """Clean a title to satisfy YouTube's 100-char limit and disallowed character rules."""
    cleaned = raw_title.replace("<", "").replace(">", "").strip()
    if not cleaned:
        cleaned = "Untitled Video"
    if len(cleaned) > 100:
        cleaned = cleaned[:97].rstrip() + "..."
    return cleaned


class YouTubeProvider(PhotoProvider):
    """Provider for synchronizing video files and album playlists with a YouTube account.

    - Uploads videos using YouTube Data API v3 resumable uploads (`videos().insert`).
    - Defaults to `privacy_status="private"` for personal video libraries.
    - Maps crosspix album names to YouTube playlists (`playlists().insert` + `playlistItems().insert`).
    - Embeds `crosspix_sha1: <sha1_hex>` in each uploaded video's description so re-runs
      deduplicate and resume cleanly without re-uploading existing videos.
    """

    platform_name = "youtube"

    def __init__(
        self,
        client_secret_file: str | Path = "youtube_oauth.json",
        fallback_secret_file: str | Path = "gphoto_oauth.json",
        token_file: str | Path = "youtube.token",
        privacy_status: PrivacyStatus = "private",
        category_id: str = "22",  # People & Blogs
        service: Any = None,
        on_progress: Optional[Callable[[str], None]] = _default_log,
    ) -> None:
        self.client_secret_file = Path(client_secret_file).expanduser()
        self.fallback_secret_file = Path(fallback_secret_file).expanduser()
        self.token_file = Path(token_file).expanduser()
        self.privacy_status: PrivacyStatus = privacy_status
        self.category_id = category_id
        self.service = service
        self.credentials: Any = None
        self.on_progress = on_progress
        self._playlist_cache: dict[str, str] = {}
        self._playlist_videos_cache: dict[str, set[str]] = {}
        self._playlists_loaded: bool = False
        self._videos_by_sha1: dict[str, PhotoItem] = {}
        self._videos_by_filename: dict[str, PhotoItem] = {}
        self._videos_loaded: bool = False

    def _log(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    def _reset_service(self) -> None:
        """Rebuild the YouTube API client to drop stale keep-alive TLS sockets."""
        if self.credentials is not None:
            if self.credentials.expired and self.credentials.refresh_token:
                self.credentials.refresh(Request())
            self.service = build("youtube", "v3", credentials=self.credentials)

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError, BrokenPipeError)):
            return True
        if isinstance(exc, HttpError):
            status = getattr(getattr(exc, "resp", None), "status", None)
            return status in {500, 502, 503, 504}
        if isinstance(exc, OSError):
            msg = str(exc).lower()
            return any(
                token in msg
                for token in ("eof", "ssl", "reset", "broken pipe", "timed out", "connection")
            )
        return False

    def _execute_with_retry(
        self,
        request_factory: Callable[[], Any],
        max_retries: int = 4,
    ) -> Any:
        """Execute a YouTube API request with automatic reconnect and backoff on transient TLS/network errors."""
        for attempt in range(max_retries + 1):
            try:
                return request_factory().execute()
            except Exception as exc:
                if attempt >= max_retries or not self._is_transient_error(exc):
                    raise
                delay = min(2**attempt, 10)
                self._log(
                    f"  [YouTube] Transient connection error ({exc}); reconnecting in {delay}s "
                    f"(retry {attempt + 1}/{max_retries})..."
                )
                time.sleep(delay)
                self._reset_service()
        return None

    def _resolve_secret_file(self) -> Path:
        if self.client_secret_file.exists():
            return self.client_secret_file
        if self.fallback_secret_file.exists():
            return self.fallback_secret_file
        raise FileNotFoundError(
            f"YouTube OAuth client secrets file not found ('{self.client_secret_file}' "
            f"or '{self.fallback_secret_file}'). Download a Desktop OAuth client JSON "
            "from Google Cloud Console with YouTube Data API v3 enabled."
        )

    def connect(self) -> None:
        """Authenticate with YouTube Data API v3 via OAuth2 InstalledAppFlow (cached in youtube.token)."""
        if self.service is not None:
            return

        creds = None
        if self.token_file.exists():
            try:
                with self.token_file.open("rb") as fh:
                    creds = pickle.load(fh)
            except Exception:
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                self._log("[YouTube] Refreshing OAuth token...")
                creds.refresh(Request())
            else:
                secret_path = self._resolve_secret_file()
                self._log(
                    f"[YouTube] Opening browser for YouTube OAuth authorization using '{secret_path}'..."
                )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(secret_path), scopes=YOUTUBE_SCOPES
                )
                creds = flow.run_local_server(port=0)
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            with self.token_file.open("wb") as fh:
                pickle.dump(creds, fh)

        self.credentials = creds
        self.service = build("youtube", "v3", credentials=creds)
        self._log("[YouTube] Connected to YouTube Data API v3.")

    def prefetch_playlists(self) -> dict[str, str]:
        """Fetch and cache all YouTube playlists owned by the authenticated channel."""
        self.connect()
        if self._playlists_loaded:
            return dict(self._playlist_cache)

        page_token: Optional[str] = None
        while True:
            resp = (
                self._execute_with_retry(
                    lambda pt=page_token: self.service.playlists().list(
                        part="snippet",
                        mine=True,
                        maxResults=50,
                        pageToken=pt,
                    )
                )
                or {}
            )
            for pl in resp.get("items") or []:
                pl_id = pl.get("id")
                title = (pl.get("snippet") or {}).get("title")
                if pl_id and title:
                    self._playlist_cache[str(title)] = str(pl_id)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        self._playlists_loaded = True
        return dict(self._playlist_cache)

    def _resolve_playlist_id(
        self, playlist_name: str, create_if_missing: bool = False
    ) -> Optional[str]:
        """Resolve a playlist title to its YouTube playlist ID, optionally creating it."""
        if not self._playlists_loaded:
            self.prefetch_playlists()

        if playlist_name in self._playlist_cache:
            return self._playlist_cache[playlist_name]

        if not create_if_missing:
            return None

        resp = (
            self._execute_with_retry(
                lambda: self.service.playlists().insert(
                    part="snippet,status",
                    body={
                        "snippet": {
                            "title": _sanitize_youtube_title(playlist_name),
                            "description": f"Album synchronized by crosspix: {playlist_name}",
                        },
                        "status": {
                            "privacyStatus": self.privacy_status,
                        },
                    },
                )
            )
            or {}
        )
        pl_id = resp.get("id")
        if pl_id:
            pl_id_str = str(pl_id)
            self._playlist_cache[playlist_name] = pl_id_str
            self._playlist_videos_cache[pl_id_str] = set()
            return pl_id_str
        return None

    def get_playlist_video_ids(self, playlist_id: str) -> set[str]:
        """Return (and cache) the set of video IDs already in a YouTube playlist."""
        self.connect()
        if playlist_id in self._playlist_videos_cache:
            return self._playlist_videos_cache[playlist_id]

        video_ids: set[str] = set()
        page_token: Optional[str] = None
        while True:
            resp = (
                self._execute_with_retry(
                    lambda pt=page_token: self.service.playlistItems().list(
                        part="contentDetails",
                        playlistId=playlist_id,
                        maxResults=50,
                        pageToken=pt,
                    )
                )
                or {}
            )
            for entry in resp.get("items") or []:
                vid = (entry.get("contentDetails") or {}).get("videoId")
                if vid:
                    video_ids.add(str(vid))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        self._playlist_videos_cache[playlist_id] = video_ids
        return video_ids

    def add_video_to_playlist(self, video_id: str, playlist_name: str) -> bool:
        """Add `video_id` to the named playlist if not already present. Returns True if newly added."""
        self.connect()
        if not video_id or not playlist_name:
            return False
        pl_id = self._resolve_playlist_id(playlist_name, create_if_missing=True)
        if not pl_id:
            return False
        existing = self.get_playlist_video_ids(pl_id)
        if video_id in existing:
            return False

        self._execute_with_retry(
            lambda: self.service.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": pl_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id,
                        },
                    }
                },
            )
        )
        existing.add(video_id)
        return True

    def prefetch_uploaded_videos(self) -> list[PhotoItem]:
        """List all videos uploaded on the channel's uploads playlist and index them by SHA-1 tag."""
        self.connect()
        if self._videos_loaded:
            seen: dict[str, PhotoItem] = {}
            for it in list(self._videos_by_sha1.values()) + list(self._videos_by_filename.values()):
                seen[it.id] = it
            return list(seen.values())

        ch_resp = (
            self._execute_with_retry(
                lambda: self.service.channels().list(part="contentDetails", mine=True)
            )
            or {}
        )
        channels = ch_resp.get("items") or []
        uploads_pl_id: Optional[str] = None
        if channels:
            uploads_pl_id = (
                (channels[0].get("contentDetails") or {})
                .get("relatedPlaylists", {})
                .get("uploads")
            )

        items: list[PhotoItem] = []
        if uploads_pl_id:
            page_token: Optional[str] = None
            while True:
                resp = (
                    self._execute_with_retry(
                        lambda pt=page_token: self.service.playlistItems().list(
                            part="snippet,contentDetails",
                            playlistId=uploads_pl_id,
                            maxResults=50,
                            pageToken=pt,
                        )
                    )
                    or {}
                )
                for entry in resp.get("items") or []:
                    snippet = entry.get("snippet") or {}
                    details = entry.get("contentDetails") or {}
                    vid = str(
                        details.get("videoId")
                        or (snippet.get("resourceId") or {}).get("videoId")
                        or ""
                    )
                    if not vid:
                        continue
                    title = str(snippet.get("title") or vid)
                    desc = str(snippet.get("description") or "")

                    m_sha = _SHA1_TAG_RE.search(desc)
                    sha1_hex = m_sha.group(1).lower() if m_sha else None

                    m_fn = _FILENAME_TAG_RE.search(desc)
                    orig_filename = m_fn.group(1).strip() if m_fn else title

                    created_raw = details.get("videoPublishedAt") or snippet.get("publishedAt")
                    created_at: Optional[datetime] = None
                    if isinstance(created_raw, str):
                        try:
                            created_at = datetime.fromisoformat(
                                created_raw.replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                        except ValueError:
                            pass

                    photo_item = PhotoItem(
                        id=vid,
                        filename=orig_filename,
                        platform=self.platform_name,
                        checksum=sha1_hex,
                        created_at=created_at,
                        mime_type="video/mp4",
                        raw_metadata={
                            "title": title,
                            "description": desc,
                            "sha1_hex": sha1_hex,
                        },
                    )
                    items.append(photo_item)
                    if sha1_hex:
                        self._videos_by_sha1[sha1_hex] = photo_item
                    self._videos_by_filename[orig_filename.lower()] = photo_item

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        self._videos_loaded = True
        return items

    def find_existing_video(
        self, sha1_hex: Optional[str], filename: str
    ) -> Optional[PhotoItem]:
        """Look up an already-uploaded YouTube video by `crosspix_sha1` tag (or exact original filename)."""
        if not self._videos_loaded:
            self.prefetch_uploaded_videos()
        if sha1_hex:
            hit = self._videos_by_sha1.get(sha1_hex.lower())
            if hit is not None:
                return hit
        return self._videos_by_filename.get(filename.lower())

    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """List uploaded YouTube videos (optionally filtered to a single playlist name)."""
        self.connect()
        videos = self.prefetch_uploaded_videos()
        playlists = self.prefetch_playlists()

        by_id = {v.id: v for v in videos}
        for pl_name, pl_id in playlists.items():
            if album and pl_name != album:
                continue
            for vid in self.get_playlist_video_ids(pl_id):
                if vid in by_id:
                    by_id[vid].add_album(pl_name)

        if album:
            return [v for v in by_id.values() if album in v.albums]
        return list(by_id.values())

    def download_photo(self, item: PhotoItem, destination_dir: Path) -> Path:
        raise NotImplementedError(
            "YouTube Data API v3 does not support downloading original video binaries."
        )

    def upload_photo(
        self,
        file_path: Path,
        item: Optional[PhotoItem] = None,
        album: Optional[str] = None,
    ) -> PhotoItem:
        """Upload a video file to YouTube (resumable) and attach it to target playlists."""
        self.connect()
        file_path = Path(file_path).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"Video file not found: {file_path}")
        if not is_video_filename(file_path.name):
            raise ValueError(f"YouTubeProvider only supports video files, got: {file_path.name}")

        st = file_path.stat()
        filename = item.filename if item and item.filename else file_path.name
        created_at = (
            item.created_at
            if item and item.created_at
            else datetime.fromtimestamp(st.st_mtime)
        )
        sha1_hex = (
            (item.raw_metadata.get("sha1_hex") if item else None)
            or (item.checksum if item and item.checksum and len(item.checksum) == 40 else None)
        )
        if not sha1_hex:
            import hashlib

            h1 = hashlib.sha1()
            with file_path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h1.update(chunk)
            sha1_hex = h1.hexdigest()

        target_albums: list[str] = []
        if item:
            for a in item.albums:
                if a and a not in target_albums:
                    target_albums.append(a)
            if item.album and item.album not in target_albums:
                target_albums.append(item.album)
        if album and album not in target_albums:
            target_albums.append(album)

        # Build clean title and description carrying exact SHA-1 for resumable deduplication
        title_base = Path(filename).stem
        title = _sanitize_youtube_title(
            f"{created_at.strftime('%Y-%m-%d')} - {title_base}"
            if not re.match(r"^(?:19|20)\d{2}", title_base)
            else title_base
        )
        desc_lines = [
            "Uploaded by crosspix",
            f"Original filename: {filename}",
            f"Date: {created_at.isoformat()}",
            f"crosspix_sha1: {sha1_hex}",
        ]
        if target_albums:
            desc_lines.append(f"Albums: {', '.join(target_albums)}")

        mime_type, _ = mimetypes.guess_type(filename)
        media = MediaFileUpload(
            str(file_path),
            mimetype=mime_type or "video/*",
            chunksize=8 * 1024 * 1024,
            resumable=True,
        )
        body = {
            "snippet": {
                "title": title,
                "description": "\n".join(desc_lines),
                "categoryId": self.category_id,
            },
            "status": {
                "privacyStatus": self.privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

        request = self.service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        last_pct = -1
        size_mb = st.st_size / (1024 * 1024)
        chunk_retries = 0
        max_chunk_retries = 5
        while response is None:
            try:
                status, response = request.next_chunk()
                chunk_retries = 0
            except Exception as exc:
                if chunk_retries >= max_chunk_retries or not self._is_transient_error(exc):
                    raise
                chunk_retries += 1
                delay = min(2**chunk_retries, 16)
                self._log(
                    f"  [YouTube] Transient upload error on '{filename}' ({exc}); "
                    f"retrying chunk in {delay}s ({chunk_retries}/{max_chunk_retries})..."
                )
                time.sleep(delay)
                # Rebuild upload request only if session hasn't even started (resumable_uri is None)
                if getattr(request, "resumable_uri", None) is None:
                    self._reset_service()
                    request = self.service.videos().insert(
                        part="snippet,status",
                        body=body,
                        media_body=media,
                    )
                continue

            if status is not None:
                pct = int(status.progress() * 100)
                if pct >= last_pct + 10 or pct == 100:
                    self._log(
                        f"  [YouTube] Uploading '{filename}' ({size_mb:.1f} MB): {pct}%"
                    )
                    last_pct = pct

        video_id = str((response or {}).get("id", ""))
        for pl_name in target_albums:
            self.add_video_to_playlist(video_id, pl_name)

        uploaded = PhotoItem(
            id=video_id,
            filename=filename,
            platform=self.platform_name,
            checksum=sha1_hex,
            created_at=created_at,
            size=st.st_size,
            mime_type=mime_type or "video/mp4",
            album=target_albums[0] if target_albums else None,
            albums=target_albums,
            raw_metadata={
                **(response or {}),
                "sha1_hex": sha1_hex,
            },
        )
        if video_id:
            self._videos_by_sha1[sha1_hex.lower()] = uploaded
            self._videos_by_filename[filename.lower()] = uploaded
        return uploaded

    def sync_albums(self, item: PhotoItem, albums: Iterable[str]) -> PhotoItem:
        """Attach an already-uploaded YouTube video to additional playlists."""
        self.connect()
        for pl_name in albums:
            if pl_name and pl_name not in item.albums:
                self.add_video_to_playlist(item.id, pl_name)
                item.add_album(pl_name)
        return item
