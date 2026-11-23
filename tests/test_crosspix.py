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


def test_migrate_ente_directory_moves_and_hardlinks(tmp_path: Path) -> None:
    """Verify migrate_ente_directory moves files to _library/YYYY/MM/, creates album hardlinks, and removes duplicates."""
    from scripts.migrate_ente_folder import migrate_ente_directory

    ente_dir = tmp_path / "Ente"
    # Photo 1 in two albums + Uncategorized (3 physical copies initially)
    p1_uncat = ente_dir / "Uncategorized" / "cervin.jpg"
    p1_alb1 = ente_dir / "Zermatt 2015" / "cervin.jpg"
    p1_alb2 = ente_dir / "Montagne Suisse Eté" / "cervin.jpg"

    _create_sample_jpeg(p1_uncat, "2015:08:12 10:30:00", (100, 150, 220))
    p1_alb1.parent.mkdir(parents=True, exist_ok=True)
    p1_alb1.write_bytes(p1_uncat.read_bytes())
    p1_alb2.parent.mkdir(parents=True, exist_ok=True)
    p1_alb2.write_bytes(p1_uncat.read_bytes())

    # Sidecar metadata JSON inside Zermatt 2015/metadata/
    meta_dir = p1_alb1.parent / "metadata"
    meta_dir.mkdir()
    (meta_dir / "cervin.jpg.json").write_text('{"title": "cervin.jpg"}', encoding="utf-8")

    stats = migrate_ente_directory(source_dir=ente_dir, link_mode="hardlink")
    assert stats["scanned"] == 3
    assert stats["moved_to_library"] == 1
    assert stats["deduplicated_copies"] == 2
    assert stats["album_links_created"] == 2

    canonical = ente_dir / "_library" / "2015" / "08" / "cervin.jpg"
    link1 = ente_dir / "albums" / "Zermatt" / "2015" / "cervin.jpg"
    link2 = ente_dir / "albums" / "Montagne" / "Suisse" / "Eté" / "cervin.jpg"

    assert canonical.exists()
    assert link1.exists()
    assert link2.exists()
    assert canonical.stat().st_ino == link1.stat().st_ino == link2.stat().st_ino

    # Legacy flat folders should have been moved and cleaned up
    assert not p1_uncat.exists()
    assert not p1_alb1.exists()
    assert not p1_alb2.exists()
    assert not (ente_dir / "albums" / "Uncategorized").exists()


def test_migrate_ente_directory_handles_same_name_collisions_and_resume(tmp_path: Path) -> None:
    """Sequential migration disambiguates distinct files sharing a filename in YYYY/MM and deduplicates their copies."""
    from scripts.migrate_ente_folder import migrate_ente_directory

    ente_dir = tmp_path / "Ente"

    # Two distinct photos with the exact same filename and month
    img1_a = ente_dir / "Album A" / "IMG_0001.jpg"
    img2_b = ente_dir / "Album B" / "IMG_0001.jpg"
    img2_c = ente_dir / "Album C" / "IMG_0001.jpg"

    _create_sample_jpeg(img1_a, "2020:07:14 12:00:00", (200, 10, 10))
    _create_sample_jpeg(img2_b, "2020:07:14 18:00:00", (10, 10, 200))
    img2_c.parent.mkdir(parents=True, exist_ok=True)
    img2_c.write_bytes(img2_b.read_bytes())

    # Dry-run should preview moves + deduplication without touching disk
    dry_stats = migrate_ente_directory(source_dir=ente_dir, dry_run=True)
    assert dry_stats["scanned"] == 3
    assert dry_stats["moved_to_library"] == 2
    assert dry_stats["deduplicated_copies"] == 1
    assert dry_stats["album_links_created"] == 3
    assert not (ente_dir / "_library").exists()

    # Real run
    stats = migrate_ente_directory(source_dir=ente_dir, link_mode="hardlink")
    assert stats["scanned"] == 3
    assert stats["moved_to_library"] == 2
    assert stats["deduplicated_copies"] == 1
    assert stats["album_links_created"] == 3

    lib_files = sorted((ente_dir / "_library" / "2020" / "07").iterdir())
    assert len(lib_files) == 2

    link_a = next((ente_dir / "albums" / "Album" / "A").iterdir())
    link_b = next((ente_dir / "albums" / "Album" / "B").iterdir())
    link_c = next((ente_dir / "albums" / "Album" / "C").iterdir())
    assert link_b.stat().st_ino == link_c.stat().st_ino
    assert link_a.stat().st_ino != link_b.stat().st_ino

    # Adding another copy later and re-running deduplicates directly against `_library/YYYY/MM/`
    img1_d = ente_dir / "Album D" / "IMG_0001.jpg"
    img1_d.parent.mkdir(parents=True, exist_ok=True)
    img1_d.write_bytes(link_a.read_bytes())

    resume_stats = migrate_ente_directory(source_dir=ente_dir, link_mode="hardlink")
    assert resume_stats["scanned"] == 1
    assert resume_stats["moved_to_library"] == 0
    assert resume_stats["deduplicated_copies"] == 1
    assert resume_stats["album_links_created"] == 1
    link_d = next((ente_dir / "albums" / "Album" / "D").iterdir())
    assert link_d.stat().st_ino == link_a.stat().st_ino


def test_migrate_ente_directory_moves_remaining_files_and_meta_dirs(tmp_path: Path) -> None:
    """Verify remaining non-media files and hidden folders like `.meta` move into the corresponding album."""
    from scripts.migrate_ente_folder import migrate_ente_directory

    ente_dir = tmp_path / "Ente"
    album_src = ente_dir / "2026 Grand Bisse d'Ayent"

    # A photo + a leftover non-media file + a `.meta` folder with nested files
    photo = album_src / "bisse.jpg"
    _create_sample_jpeg(photo, "2026:06:15 09:30:00", (40, 120, 80))

    gpx = album_src / "parcours.gpx"
    gpx.write_text("<gpx>track</gpx>", encoding="utf-8")

    meta_dir = album_src / ".meta"
    (meta_dir / "nested").mkdir(parents=True)
    (meta_dir / "album.json").write_text('{"desc": "bisse"}', encoding="utf-8")
    (meta_dir / "nested" / "thumb.jpg").write_bytes(b"fake-thumb")

    # Dry-run should not touch disk
    dry_stats = migrate_ente_directory(source_dir=ente_dir, dry_run=True)
    assert dry_stats["extra_files_moved"] >= 3
    assert meta_dir.exists()
    assert gpx.exists()

    # Real run
    stats = migrate_ente_directory(source_dir=ente_dir, link_mode="hardlink")
    assert stats["moved_to_library"] == 1
    assert stats["album_links_created"] == 1
    assert stats["extra_files_moved"] == 3  # parcours.gpx + 2 files inside .meta

    target_album = ente_dir / "albums" / "2026" / "Grand" / "Bisse" / "d'Ayent"
    assert (target_album / "bisse.jpg").exists()
    assert (target_album / "parcours.gpx").read_text(encoding="utf-8") == "<gpx>track</gpx>"
    assert (target_album / ".meta" / "album.json").read_text(encoding="utf-8") == '{"desc": "bisse"}'
    assert (target_album / ".meta" / "nested" / "thumb.jpg").read_bytes() == b"fake-thumb"
    # Thumbnail inside `.meta` must NOT have been imported into `_library/`
    assert list((ente_dir / "_library" / "2026" / "06").iterdir()) == [
        ente_dir / "_library" / "2026" / "06" / "bisse.jpg"
    ]
    # Original album directory should be completely gone
    assert not album_src.exists()

    # Re-running when only `.meta` remains in the legacy folder merges cleanly into existing `.meta`
    (album_src / ".meta").mkdir(parents=True)
    (album_src / ".meta" / "album.json").write_text('{"desc": "bisse"}', encoding="utf-8")
    (album_src / ".meta" / "extra.txt").write_text("more", encoding="utf-8")

    merge_stats = migrate_ente_directory(source_dir=ente_dir, link_mode="hardlink")
    assert merge_stats["deduplicated_copies"] == 1
    assert merge_stats["extra_files_moved"] == 1
    assert (target_album / ".meta" / "extra.txt").read_text(encoding="utf-8") == "more"
    assert not (target_album / ".meta" / ".meta").exists()
    assert not album_src.exists()


def test_deduplicate_library_variants_priority_and_album_links(tmp_path: Path) -> None:
    """Verify `_o` > `-modifié` > plain deduplication, short renaming, album hardlink rebuilding, and multi-_o safety."""
    import os
    from scripts.deduplicate_library_variants import deduplicate_library_variants

    root = tmp_path / "Ente"
    lib_dir = root / "_library" / "2014" / "07"
    alb_vac = root / "albums" / "Vacances" / "2014"
    alb_fam = root / "albums" / "Famille"
    lib_dir.mkdir(parents=True)
    alb_vac.mkdir(parents=True)
    alb_fam.mkdir(parents=True)

    # Group 1: plain + `-modifié` + `_<id>_o` -> must keep `_o`, rename to short, rebuild ALL album links
    g1_plain = lib_dir / "20140726_104622.jpg"
    g1_mod = lib_dir / "20140726_104622-modifié.jpg"
    g1_o = lib_dir / "20140726_104622_14735170026_o.jpg"
    _create_sample_jpeg(g1_plain, "2014:07:26 10:46:22", (100, 100, 100))
    _create_sample_jpeg(g1_mod, "2014:07:26 10:46:22", (110, 110, 110))
    _create_sample_jpeg(g1_o, "2014:07:26 10:46:22", (120, 120, 120))
    g1_o_bytes = g1_o.read_bytes()
    g1_o_ino = g1_o.stat().st_ino

    # Album Vacances linked to `_o` AND plain; Album Famille linked to `-modifié`
    os.link(g1_o, alb_vac / g1_o.name)
    os.link(g1_plain, alb_vac / g1_plain.name)
    os.link(g1_mod, alb_fam / g1_mod.name)

    # Group 2: plain + `-modifié` (no `_o`) -> must keep `-modifié`, rename to short, rebuild album link
    g2_plain = lib_dir / "20140727_120000.jpg"
    g2_mod = lib_dir / "20140727_120000-modifié.jpg"
    _create_sample_jpeg(g2_plain, "2014:07:27 12:00:00", (50, 50, 200))
    _create_sample_jpeg(g2_mod, "2014:07:27 12:00:00", (50, 200, 50))
    g2_mod_bytes = g2_mod.read_bytes()
    g2_mod_ino = g2_mod.stat().st_ino
    os.link(g2_plain, alb_fam / g2_plain.name)

    # Group 3: two visually DISTINCT `_o` photos sharing the same Flickr title -> must BOTH be preserved!
    from PIL import Image, ImageDraw

    def _make_patterned(path: Path, vertical: bool) -> None:
        img = Image.new("RGB", (128, 128), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        if vertical:
            draw.rectangle([0, 0, 64, 128], fill=(240, 240, 240))
        else:
            draw.rectangle([0, 0, 128, 64], fill=(240, 240, 240))
        img.save(path, format="JPEG")

    g3_a = lib_dir / "Happy beach_19933191269_o.jpg"
    g3_b = lib_dir / "Happy beach_19933311749_o.jpg"
    _make_patterned(g3_a, vertical=True)
    _make_patterned(g3_b, vertical=False)

    # Dry-run leaves disk untouched
    dry = deduplicate_library_variants(root_dir=root, dry_run=True)
    assert dry["groups_processed"] == 2
    assert dry["files_deleted"] == 3
    assert g1_o.exists() and g1_mod.exists() and g2_mod.exists()

    # Real run
    stats = deduplicate_library_variants(root_dir=root, dry_run=False)
    assert stats["groups_processed"] == 2
    assert stats["files_deleted"] == 3
    assert stats["files_renamed"] == 2
    assert stats["distinct_preserved"] == 2

    # Group 1 checks
    g1_final = lib_dir / "20140726_104622.jpg"
    assert g1_final.exists()
    assert not g1_mod.exists()
    assert not g1_o.exists()
    assert g1_final.read_bytes() == g1_o_bytes
    assert g1_final.stat().st_ino == g1_o_ino

    # Album links for Group 1: both albums now have ONLY the short name hardlinked to g1_o_ino!
    assert (alb_vac / "20140726_104622.jpg").stat().st_ino == g1_o_ino
    assert not (alb_vac / g1_o.name).exists()
    assert (alb_fam / "20140726_104622.jpg").stat().st_ino == g1_o_ino
    assert not (alb_fam / g1_mod.name).exists()

    # Group 2 checks
    g2_final = lib_dir / "20140727_120000.jpg"
    assert g2_final.exists()
    assert not g2_mod.exists()
    assert g2_final.read_bytes() == g2_mod_bytes
    assert g2_final.stat().st_ino == g2_mod_ino
    assert (alb_fam / "20140727_120000.jpg").stat().st_ino == g2_mod_ino

    # Group 3 checks: both distinct `_o` photos untouched!
    assert g3_a.exists()
    assert g3_b.exists()

    # Phase 2 standalone repair test (--albums-only): broken symlink + orphaned nlink==1 hardlink
    broken_sym = alb_vac / "20140726_104622_14735170026_o.jpg"
    os.symlink(os.path.relpath(lib_dir / "20140726_104622_14735170026_o.jpg", alb_vac), broken_sym)
    # Remove the valid link in alb_vac first so the broken symlink is restored as the album link
    (alb_vac / "20140726_104622.jpg").unlink()

    alb_orphan = root / "albums" / "Orphans"
    alb_orphan.mkdir(parents=True)
    orphan_copy = alb_orphan / "20140727_120000-modifié.jpg"
    orphan_copy.write_bytes(b"stale-orphan-copy")

    repair_stats = deduplicate_library_variants(root_dir=root, albums_only=True)
    assert repair_stats["broken_links_repaired"] == 2
    assert (alb_vac / "20140726_104622.jpg").is_symlink()
    assert (alb_vac / "20140726_104622.jpg").resolve() == g1_final.resolve()
    assert not broken_sym.exists()
    assert (alb_orphan / "20140727_120000.jpg").stat().st_ino == g2_mod_ino
    assert not orphan_copy.exists()


def test_deduplicate_library_variants_hex8_and_paren_copies(tmp_path: Path) -> None:
    """Verify `_<8hex>` and `(N)` duplicates keep the higher-quality copy when visually identical, and preserve distinct photos."""
    import os
    from PIL import Image, ImageDraw
    from scripts.deduplicate_library_variants import deduplicate_library_variants

    root = tmp_path / "Ente"
    lib_dir = root / "_library" / "2016" / "02"
    alb_dir = root / "albums" / "Ski" / "2016"
    lib_dir.mkdir(parents=True)
    alb_dir.mkdir(parents=True)

    def _save_pattern(path: Path, vertical: bool, size: tuple[int, int], quality: int) -> None:
        img = Image.new("RGB", size, (30, 40, 50))
        draw = ImageDraw.Draw(img)
        w, h = size
        if vertical:
            draw.rectangle([0, 0, w // 2, h], fill=(220, 180, 60))
            draw.ellipse([w // 4, h // 4, 3 * w // 4, 3 * h // 4], fill=(40, 160, 200))
        else:
            draw.rectangle([0, 0, w, h // 2], fill=(200, 60, 120))
            draw.rectangle([w // 4, h // 3, 3 * w // 4, 2 * h // 3], fill=(80, 220, 100))
        img.save(path, format="JPEG", quality=quality)

    # 1. `_<8hex>` visual duplicate: `_403659de` is higher quality -> keep it, rename to short, update album link
    wa12_small = lib_dir / "IMG-20160221-WA0012.jpeg"
    wa12_large = lib_dir / "IMG-20160221-WA0012_403659de.jpeg"
    _save_pattern(wa12_small, vertical=True, size=(200, 300), quality=40)
    _save_pattern(wa12_large, vertical=True, size=(200, 300), quality=95)
    wa12_large_ino = wa12_large.stat().st_ino
    os.link(wa12_small, alb_dir / wa12_small.name)

    # 2. `(1)` visual duplicate: `(1)` is higher quality -> keep it, rename to short, update album link
    wa19_small = lib_dir / "IMG-20160221-WA0019.jpeg"
    wa19_large = lib_dir / "IMG-20160221-WA0019(1).jpeg"
    _save_pattern(wa19_small, vertical=False, size=(300, 200), quality=40)
    _save_pattern(wa19_large, vertical=False, size=(300, 200), quality=95)
    wa19_large_ino = wa19_large.stat().st_ino
    os.link(wa19_large, alb_dir / wa19_large.name)

    # 3. Visually DISTINCT photo with `_<8hex>` -> MUST be preserved!
    diff_a = lib_dir / "001.jpg"
    diff_b = lib_dir / "001_deadbeef.jpg"
    _save_pattern(diff_a, vertical=True, size=(200, 200), quality=90)
    _save_pattern(diff_b, vertical=False, size=(200, 200), quality=90)

    # 4. Date-named files `IMG_20160221.jpg` and `IMG_20160222.jpg` -> MUST NOT be treated as `_<8hex>`!
    date_a = lib_dir / "IMG_20160221.jpg"
    date_b = lib_dir / "IMG_20160222.jpg"
    _save_pattern(date_a, vertical=True, size=(200, 200), quality=90)
    _save_pattern(date_b, vertical=True, size=(200, 200), quality=90)

    stats = deduplicate_library_variants(root_dir=root)
    assert stats["groups_processed"] == 2
    assert stats["files_deleted"] == 2
    assert stats["files_renamed"] == 2
    assert stats["distinct_preserved"] == 1

    # WA0012: kept high-quality `_403659de`, renamed to short, album link updated to same inode
    assert (lib_dir / "IMG-20160221-WA0012.jpeg").stat().st_ino == wa12_large_ino
    assert not wa12_large.exists()
    assert (alb_dir / "IMG-20160221-WA0012.jpeg").stat().st_ino == wa12_large_ino

    # WA0019: kept high-quality `(1)`, renamed to short, album link renamed to short
    assert (lib_dir / "IMG-20160221-WA0019.jpeg").stat().st_ino == wa19_large_ino
    assert not wa19_large.exists()
    assert (alb_dir / "IMG-20160221-WA0019.jpeg").stat().st_ino == wa19_large_ino
    assert not (alb_dir / "IMG-20160221-WA0019(1).jpeg").exists()

    # Distinct `001` pair & date-named pair completely preserved
    assert diff_a.exists() and diff_b.exists()
    assert date_a.exists() and date_b.exists()


def test_local_albums_only_and_immich_url_normalization(tmp_path: Path) -> None:
    """Verify LocalProvider(albums_only=True) skips `_library/`-only photos and `.meta/` while keeping multi-album links."""
    import os

    root = tmp_path / "Ente"
    lib_only = root / "_library" / "2024" / "05" / "unlinked.jpg"
    lib_linked = root / "_library" / "2024" / "05" / "shared.jpg"
    _create_sample_jpeg(lib_only, "2024:05:01 10:00:00", (10, 20, 30))
    _create_sample_jpeg(lib_linked, "2024:05:02 11:00:00", (40, 50, 60))

    alb1 = root / "albums" / "Voyage" / "Bali"
    alb2 = root / "albums" / "Famille"
    meta = alb1 / ".meta"
    alb1.mkdir(parents=True)
    alb2.mkdir(parents=True)
    meta.mkdir(parents=True)
    os.link(lib_linked, alb1 / "shared.jpg")
    os.link(lib_linked, alb2 / "shared.jpg")
    _create_sample_jpeg(meta / "thumb.jpg", "2024:05:02 11:00:00", (70, 80, 90))

    local = LocalProvider(root_dir=root, albums_only=True)
    items = list(local.list_photos())
    assert len(items) == 1
    assert items[0].filename == "shared.jpg"
    assert items[0].albums == ["Famille", "Voyage Bali"]
    assert "sha1_b64" in items[0].raw_metadata

    # ImmichProvider normalizes URLs without `/api` and matches local items by Immich SHA-1 Base64 checksum
    immich = ImmichProvider(base_url="http://localhost:2283", api_key="k")
    assert immich.base_url == "http://localhost:2283/api"
    remote_item = PhotoItem(
        id="uuid-1",
        filename="renamed_on_immich.jpg",
        platform="immich",
        checksum=items[0].raw_metadata["sha1_b64"],
    )
    assert items[0].matches(remote_item)








