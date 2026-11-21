"""Base models and abstract provider interface for crosspix."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass
class PhotoItem:
    """Universal representation of a photo or media asset across platforms."""

    id: str
    filename: str
    platform: str
    checksum: Optional[str] = None
    created_at: Optional[datetime] = None
    size: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    mime_type: str = "image/jpeg"
    album: Optional[str] = None
    albums: list[str] = field(default_factory=list)
    local_path: Optional[Path] = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.album and self.album not in self.albums:
            self.albums.insert(0, self.album)
        elif self.albums and self.album is None:
            self.album = self.albums[0]

    def add_album(self, album_name: Optional[str]) -> None:
        """Associate an album name with this photo if not already present."""
        if not album_name:
            return
        if album_name not in self.albums:
            self.albums.append(album_name)
        if self.album is None:
            self.album = album_name

    def matches(self, other: "PhotoItem") -> bool:
        """Return True if two PhotoItems represent the same underlying photo."""
        self_hashes = {
            h.lower()
            for h in (self.checksum, self.raw_metadata.get("sha512_b64"))
            if isinstance(h, str) and h
        }
        other_hashes = {
            h.lower()
            for h in (other.checksum, other.raw_metadata.get("sha512_b64"))
            if isinstance(h, str) and h
        }
        if self_hashes and other_hashes and (self_hashes & other_hashes):
            return True
        if self.filename.lower() == other.filename.lower():
            if self.size and other.size and self.size == other.size:
                return True
            if self.created_at and other.created_at:
                # Match within 2 seconds to tolerate filesystem/API timestamp rounding
                delta = abs((self.created_at - other.created_at).total_seconds())
                if delta <= 2.0:
                    return True
            if self.created_at is None and other.created_at is None:
                return True
        return False


class PhotoProvider(ABC):
    """Abstract base class implemented by all photo storage providers."""

    platform_name: str = "abstract"

    @abstractmethod
    def connect(self) -> None:
        """Authenticate and initialize the connection to the provider."""

    @abstractmethod
    def list_photos(self, album: Optional[str] = None) -> Iterable[PhotoItem]:
        """Yield PhotoItem objects available on the provider."""

    @abstractmethod
    def download_photo(self, item: PhotoItem, destination_dir: Path) -> Path:
        """Download a PhotoItem into destination_dir and return the local file Path."""

    @abstractmethod
    def upload_photo(
        self,
        file_path: Path,
        item: Optional[PhotoItem] = None,
        album: Optional[str] = None,
    ) -> PhotoItem:
        """Upload a local photo file to the provider and return the created PhotoItem."""

    def sync_albums(self, item: PhotoItem, albums: Iterable[str]) -> PhotoItem:
        """Attach an existing PhotoItem on this provider to additional albums without re-uploading."""
        for alb in albums:
            item.add_album(alb)
        return item

    def close(self) -> None:
        """Clean up any active session or resources."""
        return None

    def __enter__(self) -> "PhotoProvider":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
