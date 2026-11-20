"""Unit tests for crosspix providers and synchronization engine."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import piexif
from PIL import Image

from crosspix import (
    EnteProvider,
    GooglePhotosProvider,
    ImmichProvider,
    LocalProvider,
    PhotoItem,
    PhotoSynchronizer,
)


def _create_sample_jpeg(path: Path, dt_str: str = "2026:07:15 14:30:00", color: tuple[int, int, int] = (40, 120, 200)) -> Path:
    """Create a real JPEG image with EXIF DateTimeOriginal using Pillow and piexif."""
    img = Image.new("RGB", (64, 48), color=color)
    exif_dict = {
        "0th": {piexif.ImageIFD.Make: b"CrosspixCam"},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: dt_str.encode("ascii")},
    }
    exif_bytes = piexif.dump(exif_dict)
    img.save(path, "jpeg", exif=exif_bytes)
    return path


def test_local_provider_exif_and_sync(tmp_path: Path) -> None:
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "target"
    src_dir.mkdir()
    dst_dir.mkdir()

    _create_sample_jpeg(src_dir / "holiday1.jpg", "2026:08:01 10:15:30", (200, 50, 50))
    _create_sample_jpeg(src_dir / "holiday2.jpg", "2026:08:02 18:45:00", (50, 200, 50))

    src_provider = LocalProvider(src_dir)
    dst_provider = LocalProvider(dst_dir)

    items = list(src_provider.list_photos())
    assert len(items) == 2
    assert items[0].width == 64
    assert items[0].height == 48
    assert items[0].created_at == datetime(2026, 8, 1, 10, 15, 30)
    assert items[0].checksum is not None

    synchronizer = PhotoSynchronizer()
    res1 = synchronizer.sync(src_provider, dst_provider)
    assert len(res1.copied) == 2
    assert len(res1.skipped) == 0
    assert (dst_dir / "holiday1.jpg").exists()
    assert (dst_dir / "holiday2.jpg").exists()

    # Running sync a second time should deduplicate and skip both photos
    res2 = synchronizer.sync(src_provider, dst_provider)
    assert len(res2.copied) == 0
    assert len(res2.skipped) == 2


def test_bidirectional_local_sync(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    _create_sample_jpeg(dir_a / "from_a.jpg", "2026:05:10 09:00:00", (10, 20, 30))
    _create_sample_jpeg(dir_b / "from_b.jpg", "2026:05:11 11:00:00", (30, 20, 10))

    prov_a = LocalProvider(dir_a)
    prov_b = LocalProvider(dir_b)

    synchronizer = PhotoSynchronizer()
    res_ab, res_ba = synchronizer.bisync(prov_a, prov_b)

    assert len(res_ab.copied) == 1
    assert len(res_ba.copied) == 1
    assert (dir_a / "from_b.jpg").exists()
    assert (dir_b / "from_a.jpg").exists()


def test_ente_provider_encryption_and_export_mirror(tmp_path: Path) -> None:
    ente_dir = tmp_path / "ente_export"
    ente = EnteProvider(export_dir=ente_dir)
    ente.connect()

    # Test PyNaCl E2EE encryption/decryption round-trip
    plaintext = b"sample-jpeg-binary-content"
    ciphertext, nonce = ente.encrypt_payload(plaintext)
    assert ciphertext != plaintext
    assert ente.decrypt_payload(ciphertext, nonce) == plaintext

    # Test upload/list through export_dir mirror
    sample_img = _create_sample_jpeg(tmp_path / "ente_test.jpg")
    uploaded = ente.upload_photo(sample_img)
    assert uploaded.platform == "ente"
    listed = list(ente.list_photos())
    assert len(listed) == 1
    assert listed[0].filename == "ente_test.jpg"


def test_google_photos_provider_upload_and_download(tmp_path: Path) -> None:
    mock_session = MagicMock()

    # Mock upload token response and batchCreate response
    upload_resp = MagicMock()
    upload_resp.text = "mock-upload-token-123"
    upload_resp.raise_for_status = MagicMock()

    create_resp = MagicMock()
    create_resp.json.return_value = {
        "newMediaItemResults": [
            {
                "mediaItem": {
                    "id": "gphoto-id-1",
                    "filename": "sunset.jpg",
                    "mimeType": "image/jpeg",
                    "baseUrl": "https://lh3.googleusercontent.com/mock-photo",
                    "mediaMetadata": {
                        "creationTime": "2026-06-01T12:00:00Z",
                        "width": "64",
                        "height": "48",
                    },
                }
            }
        ]
    }
    create_resp.raise_for_status = MagicMock()
    mock_session.post.side_effect = [upload_resp, create_resp]

    download_resp = MagicMock()
    download_resp.content = b"downloaded-jpeg-bytes"
    download_resp.raise_for_status = MagicMock()
    mock_session.get.return_value = download_resp

    provider = GooglePhotosProvider(access_token="fake-oauth-token", session=mock_session)
    provider.connect()

    sample_file = _create_sample_jpeg(tmp_path / "sunset.jpg")
    item = provider.upload_photo(sample_file)
    assert item.id == "gphoto-id-1"
    assert item.filename == "sunset.jpg"
    assert item.width == 64

    dl_path = provider.download_photo(item, tmp_path / "downloads")
    assert dl_path.read_bytes() == b"downloaded-jpeg-bytes"


def test_immich_provider_upload_and_download(tmp_path: Path) -> None:
    mock_http = MagicMock()

    post_resp = MagicMock()
    post_resp.json.return_value = {"id": "immich-asset-42", "status": "created"}
    post_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = post_resp

    get_resp = MagicMock()
    get_resp.content = b"immich-original-bytes"
    get_resp.raise_for_status = MagicMock()
    mock_http.get.return_value = get_resp

    provider = ImmichProvider(
        base_url="http://localhost:2283/api",
        api_key="test-immich-key",
        http_client=mock_http,
    )
    provider.connect()

    sample_file = _create_sample_jpeg(tmp_path / "immich_pic.jpg")
    uploaded = provider.upload_photo(sample_file)
    assert uploaded.id == "immich-asset-42"
    assert uploaded.platform == "immich"

    downloaded = provider.download_photo(uploaded, tmp_path / "immich_dl")
    assert downloaded.read_bytes() == b"immich-original-bytes"
