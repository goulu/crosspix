"""crosspix: Cross-platform photo synchronization across Google Photos, Ente, Immich, and local folders."""

from crosspix.base import PhotoItem, PhotoProvider
from crosspix.ente import EnteProvider
from crosspix.google_photos import GooglePhotosProvider
from crosspix.immich import ImmichProvider
from crosspix.local import LocalProvider
from crosspix.sync import PhotoSynchronizer, SyncResult
from crosspix.youtube import YouTubeProvider

__version__ = "0.1.0"

__all__ = [
    "PhotoItem",
    "PhotoProvider",
    "LocalProvider",
    "GooglePhotosProvider",
    "EnteProvider",
    "ImmichProvider",
    "YouTubeProvider",
    "PhotoSynchronizer",
    "SyncResult",
]
