"""Unit tests for crosspix providers and synchronization engine."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

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


def _create_sample_jpeg(
    path: Path,
    dt_str: str = "2026:07:15 14:30:00",
    color: tuple[int, int, int] = (40, 120, 200),
) -> Path:
    """Create a real JPEG image with EXIF DateTimeOriginal using Pillow and piexif."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
    assert (dst_dir / "_library" / "2026" / "08" / "holiday1.jpg").exists()
    assert (dst_dir / "_library" / "2026" / "08" / "holiday2.jpg").exists()

    # Running sync a second time should deduplicate and skip both photos
    res2 = synchronizer.sync(src_provider, dst_provider)
    assert len(res2.copied) == 0
    assert len(res2.skipped) == 2


def test_multi_album_space_hierarchy_and_hardlink_deduplication(tmp_path: Path) -> None:
    """Verify Option 2: _library/YYYY/MM/ canonical storage + space-delimited albums/ hierarchy via hardlinks."""
    local_root = tmp_path / "photos"
    provider = LocalProvider(local_root, album_delimiter=" ", link_mode="hardlink")
    provider.connect()

    raw_img = _create_sample_jpeg(tmp_path / "rome.jpg", "2026:08:15 12:00:00", (180, 90, 40))
    src_item = PhotoItem(
        id="gphoto-rome-1",
        filename="rome.jpg",
        platform="google_photos",
        created_at=datetime(2026, 8, 15, 12, 0, 0),
        albums=["Vacances 2026 Italie", "Famille Eté Favoris"],
    )

    uploaded = provider.upload_photo(raw_img, item=src_item)

    canonical_file = local_root / "_library" / "2026" / "08" / "rome.jpg"
    album_path_1 = local_root / "albums" / "Vacances" / "2026" / "Italie" / "rome.jpg"
    album_path_2 = local_root / "albums" / "Famille" / "Eté" / "Favoris" / "rome.jpg"

    assert canonical_file.exists()
    assert album_path_1.exists()
    assert album_path_2.exists()

    # Verify all 3 paths share the exact same inode (0 duplicate bytes on disk)
    inode_canonical = canonical_file.stat().st_ino
    assert album_path_1.stat().st_ino == inode_canonical
    assert album_path_2.stat().st_ino == inode_canonical
    assert sorted(uploaded.albums) == ["Famille Eté Favoris", "Vacances 2026 Italie"]

    # Listing photos from LocalProvider must deduplicate the 3 hardlinks into 1 PhotoItem
    listed = list(provider.list_photos())
    assert len(listed) == 1
    assert listed[0].filename == "rome.jpg"
    assert listed[0].local_path == canonical_file
    assert sorted(listed[0].albums) == ["Famille Eté Favoris", "Vacances 2026 Italie"]

    # Adding a third album afterwards via sync_albums must create a new hardlink without copying
    provider.sync_albums(listed[0], ["Architecture Europe Rome"])
    album_path_3 = local_root / "albums" / "Architecture" / "Europe" / "Rome" / "rome.jpg"
    assert album_path_3.exists()
    assert album_path_3.stat().st_ino == inode_canonical


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
    assert (dir_a / "_library" / "2026" / "05" / "from_b.jpg").exists()
    assert (dir_b / "_library" / "2026" / "05" / "from_a.jpg").exists()


def test_ente_provider_multi_album_export_and_api(tmp_path: Path) -> None:
    # 1. Test Ente export_dir mode with multi-album hardlinks & space hierarchy
    ente_dir = tmp_path / "ente_export"
    ente = EnteProvider(export_dir=ente_dir, album_delimiter=" ", link_mode="hardlink")
    ente.connect()

    plaintext = b"sample-jpeg-binary-content"
    ciphertext, nonce = ente.encrypt_payload(plaintext)
    assert ciphertext != plaintext
    assert ente.decrypt_payload(ciphertext, nonce) == plaintext

    sample_img = _create_sample_jpeg(tmp_path / "ente_test.jpg", "2026:08:20 15:00:00")
    src_item = PhotoItem(
        id="local-ente-1",
        filename="ente_test.jpg",
        platform="local",
        created_at=datetime(2026, 8, 20, 15, 0, 0),
        albums=["Vacances 2026 Suisse", "Montagne Alpes"],
    )
    uploaded = ente.upload_photo(sample_img, item=src_item)
    assert uploaded.platform == "ente"
    assert sorted(uploaded.albums) == ["Montagne Alpes", "Vacances 2026 Suisse"]

    canonical = ente_dir / "_library" / "2026" / "08" / "ente_test.jpg"
    link1 = ente_dir / "albums" / "Vacances" / "2026" / "Suisse" / "ente_test.jpg"
    link2 = ente_dir / "albums" / "Montagne" / "Alpes" / "ente_test.jpg"
    assert canonical.exists() and link1.exists() and link2.exists()
    assert canonical.stat().st_ino == link1.stat().st_ino == link2.stat().st_ino

    listed = list(ente.list_photos())
    assert len(listed) == 1
    assert sorted(listed[0].albums) == ["Montagne Alpes", "Vacances 2026 Suisse"]

    # 2. Test Ente Museum API mode deduplicating across multiple collections
    mock_http = MagicMock()
    cols_resp = MagicMock()
    cols_resp.json.return_value = {
        "collections": [
            {"id": 10, "name": "Vacances 2026 Suisse"},
            {"id": 20, "name": "Montagne Alpes"},
        ]
    }
    cols_resp.raise_for_status = MagicMock()

    diff_10 = MagicMock()
    diff_10.json.return_value = {
        "diff": [
            {
                "id": 999,
                "metadata": {"title": "cervin.jpg", "hash": "sha256-cervin", "creationTime": 1787238000000000},
            }
        ]
    }
    diff_10.raise_for_status = MagicMock()

    diff_20 = MagicMock()
    diff_20.json.return_value = {
        "diff": [
            {
                "id": 999,
                "metadata": {"title": "cervin.jpg", "hash": "sha256-cervin", "creationTime": 1787238000000000},
            }
        ]
    }
    diff_20.raise_for_status = MagicMock()

    mock_http.get.side_effect = [cols_resp, diff_10, diff_20]

    ente_api = EnteProvider(auth_token="fake-ente-token", http_client=mock_http)
    api_items = list(ente_api.list_photos())
    assert len(api_items) == 1
    assert api_items[0].filename == "cervin.jpg"
    assert api_items[0].albums == ["Vacances 2026 Suisse", "Montagne Alpes"]


def test_google_photos_provider_multi_album_upload_and_download(tmp_path: Path) -> None:
    mock_session = MagicMock()

    upload_resp = MagicMock()
    upload_resp.text = "mock-upload-token-123"
    upload_resp.raise_for_status = MagicMock()

    create_album_1_resp = MagicMock()
    create_album_1_resp.json.return_value = {"id": "album-id-1", "title": "Vacances 2026 Italie"}
    create_album_1_resp.raise_for_status = MagicMock()

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

    create_album_2_resp = MagicMock()
    create_album_2_resp.json.return_value = {"id": "album-id-2", "title": "Famille Eté"}
    create_album_2_resp.raise_for_status = MagicMock()

    batch_add_resp = MagicMock()
    batch_add_resp.raise_for_status = MagicMock()

    mock_session.post.side_effect = [
        upload_resp,
        create_album_1_resp,
        create_resp,
        create_album_2_resp,
        batch_add_resp,
    ]

    download_resp = MagicMock()
    download_resp.content = b"downloaded-jpeg-bytes"
    download_resp.raise_for_status = MagicMock()
    mock_session.get.return_value = download_resp

    provider = GooglePhotosProvider(access_token="fake-oauth-token", session=mock_session)
    provider.connect()

    sample_file = _create_sample_jpeg(tmp_path / "sunset.jpg")
    src_item = PhotoItem(
        id="local-1",
        filename="sunset.jpg",
        platform="local",
        albums=["Vacances 2026 Italie", "Famille Eté"],
    )
    item = provider.upload_photo(sample_file, item=src_item)
    assert item.id == "gphoto-id-1"
    assert item.filename == "sunset.jpg"
    assert item.albums == ["Vacances 2026 Italie", "Famille Eté"]

    dl_path = provider.download_photo(item, tmp_path / "downloads")
    assert dl_path.read_bytes() == b"downloaded-jpeg-bytes"


def test_immich_provider_multi_album_upload_and_sync_albums(tmp_path: Path) -> None:
    mock_http = MagicMock()

    # 1. POST /assets (single binary upload)
    upload_asset_resp = MagicMock()
    upload_asset_resp.json.return_value = {"id": "immich-asset-42", "status": "created"}
    upload_asset_resp.raise_for_status = MagicMock()

    # 2. GET /albums (initially empty)
    get_albums_resp = MagicMock()
    get_albums_resp.json.return_value = []
    get_albums_resp.raise_for_status = MagicMock()

    # 3. POST /albums for "Vacances 2026 Italie" and "Famille Eté"
    create_alb1_resp = MagicMock()
    create_alb1_resp.json.return_value = {"id": "immich-alb-1", "albumName": "Vacances 2026 Italie"}
    create_alb1_resp.raise_for_status = MagicMock()

    create_alb2_resp = MagicMock()
    create_alb2_resp.json.return_value = {"id": "immich-alb-2", "albumName": "Famille Eté"}
    create_alb2_resp.raise_for_status = MagicMock()

    create_alb3_resp = MagicMock()
    create_alb3_resp.json.return_value = {"id": "immich-alb-3", "albumName": "Favoris 2026"}
    create_alb3_resp.raise_for_status = MagicMock()

    # 4. PUT /albums/{id}/assets
    put_assets_resp = MagicMock()
    put_assets_resp.raise_for_status = MagicMock()
    mock_http.put.return_value = put_assets_resp

    # 5. GET /assets/{id}/original
    download_resp = MagicMock()
    download_resp.content = b"immich-original-bytes"
    download_resp.raise_for_status = MagicMock()

    mock_http.get.side_effect = [
        get_albums_resp,
        get_albums_resp,
        download_resp,
        get_albums_resp,
    ]
    mock_http.post.side_effect = [
        upload_asset_resp,
        create_alb1_resp,
        create_alb2_resp,
        create_alb3_resp,
    ]

    provider = ImmichProvider(
        base_url="http://localhost:2283/api",
        api_key="test-immich-key",
        http_client=mock_http,
    )
    provider.connect()

    sample_file = _create_sample_jpeg(tmp_path / "immich_pic.jpg")
    src_item = PhotoItem(
        id="local-immich-1",
        filename="immich_pic.jpg",
        platform="local",
        albums=["Vacances 2026 Italie", "Famille Eté"],
    )
    uploaded = provider.upload_photo(sample_file, item=src_item)
    assert uploaded.id == "immich-asset-42"
    assert uploaded.platform == "immich"
    assert uploaded.albums == ["Vacances 2026 Italie", "Famille Eté"]
    assert mock_http.put.call_count == 2

    downloaded = provider.download_photo(uploaded, tmp_path / "immich_dl")
    assert downloaded.read_bytes() == b"immich-original-bytes"

    # Incremental album sync without re-uploading the asset
    provider.sync_albums(uploaded, ["Favoris 2026"])
    assert "Favoris 2026" in uploaded.albums
    assert mock_http.put.call_count == 3


def test_in_place_ente_export_deduplication_and_hierarchy(tmp_path: Path) -> None:
    """When Ente exports directly into /home/goulu/Images/Photos, crosspix deduplicates in-place."""
    photos_dir = tmp_path / "Photos"
    # Simulate Ente CLI exporting the same photo into two flat album folders (two different inodes)
    flat_alb1 = photos_dir / "Vacances 2026 Italie" / "colisee.jpg"
    flat_alb2 = photos_dir / "Famille Eté" / "colisee.jpg"
    _create_sample_jpeg(flat_alb1, "2026:08:10 14:00:00", (120, 80, 60))
    flat_alb2.parent.mkdir(parents=True, exist_ok=True)
    flat_alb2.write_bytes(flat_alb1.read_bytes())

    assert flat_alb1.stat().st_ino != flat_alb2.stat().st_ino

    ente_src = EnteProvider(export_dir=photos_dir, album_delimiter=" ", link_mode="hardlink")
    local_dst = LocalProvider(photos_dir, album_delimiter=" ", link_mode="hardlink")

    synchronizer = PhotoSynchronizer()
    res = synchronizer.sync(ente_src, local_dst)

    assert len(res.albums_updated) == 1
    canonical = photos_dir / "_library" / "2026" / "08" / "colisee.jpg"
    hier_alb1 = photos_dir / "albums" / "Vacances" / "2026" / "Italie" / "colisee.jpg"
    hier_alb2 = photos_dir / "albums" / "Famille" / "Eté" / "colisee.jpg"

    assert canonical.exists()
    assert hier_alb1.exists()
    assert hier_alb2.exists()

    # All 5 paths (canonical, 2 hierarchical album links, and 2 flat Ente folders) now share 1 single inode!
    ino = canonical.stat().st_ino
    assert hier_alb1.stat().st_ino == ino
    assert hier_alb2.stat().st_ino == ino
    assert flat_alb1.stat().st_ino == ino
    assert flat_alb2.stat().st_ino == ino

