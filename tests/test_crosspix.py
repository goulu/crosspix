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
    assert listed[0].local_path is not None
    assert listed[0].local_path.resolve() == canonical_file.resolve()
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
    can_symlink = True
    try:
        os.symlink(
            os.path.relpath(lib_dir / "20140726_104622_14735170026_o.jpg", alb_vac),
            broken_sym,
        )
        # Remove the valid link in alb_vac first so the broken symlink is restored as the album link
        (alb_vac / "20140726_104622.jpg").unlink()
    except OSError:
        # Non-admin Windows environments may disallow symlinks
        can_symlink = False

    alb_orphan = root / "albums" / "Orphans"
    alb_orphan.mkdir(parents=True)
    orphan_copy = alb_orphan / "20140727_120000-modifié.jpg"
    orphan_copy.write_bytes(b"stale-orphan-copy")

    repair_stats = deduplicate_library_variants(root_dir=root, albums_only=True)
    assert repair_stats["broken_links_repaired"] == (2 if can_symlink else 1)
    if can_symlink:
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


def test_streaming_local_to_immich_sync_and_resume(tmp_path: Path) -> None:
    """Verify streaming local->Immich sync uploads progressively, skips _library-only files, and resumes cleanly."""
    import os

    root = tmp_path / "Ente"
    lib_only = root / "_library" / "2024" / "05" / "unlinked.jpg"
    lib_shared = root / "_library" / "2024" / "05" / "shared.jpg"
    lib_solo = root / "_library" / "2024" / "05" / "solo.jpg"
    _create_sample_jpeg(lib_only, "2024:05:01 10:00:00", (10, 10, 10))
    _create_sample_jpeg(lib_shared, "2024:05:02 11:00:00", (40, 80, 120))
    _create_sample_jpeg(lib_solo, "2024:05:03 12:00:00", (200, 100, 50))

    alb1 = root / "albums" / "AlbumA"
    alb2 = root / "albums" / "AlbumB"
    alb1.mkdir(parents=True)
    alb2.mkdir(parents=True)
    os.link(lib_shared, alb1 / "shared.jpg")
    os.link(lib_solo, alb1 / "solo.jpg")
    os.link(lib_shared, alb2 / "shared.jpg")

    # Stateful fake Immich HTTP server supporting /albums, /assets/bulk-upload-check, /assets, /albums/{id}/assets
    uploaded_by_sha1: dict[str, str] = {}
    albums_by_name: dict[str, str] = {}
    album_assets: dict[str, set[str]] = {}
    next_asset_num = 1
    next_album_num = 1

    def fake_get(url: str, headers=None, params=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/albums"):
            resp.json.return_value = [
                {"id": aid, "albumName": name} for name, aid in albums_by_name.items()
            ]
        elif "/albums/" in url:
            aid = url.rsplit("/albums/", 1)[1]
            resp.json.return_value = {
                "id": aid,
                "assets": [{"id": x} for x in sorted(album_assets.get(aid, set()))],
            }
        return resp

    def fake_post(url: str, headers=None, json=None, data=None, files=None):
        nonlocal next_asset_num, next_album_num
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/assets/bulk-upload-check"):
            results = []
            for entry in json["assets"]:
                ck = entry["checksum"]
                if ck in uploaded_by_sha1:
                    results.append(
                        {"id": entry["id"], "action": "reject", "reason": "duplicate", "assetId": uploaded_by_sha1[ck]}
                    )
                else:
                    results.append({"id": entry["id"], "action": "accept"})
            resp.json.return_value = {"results": results}
        elif url.endswith("/assets"):
            ck = (headers or {}).get("x-immich-checksum", f"ck-{next_asset_num}")
            aid = f"asset-{next_asset_num}"
            next_asset_num += 1
            uploaded_by_sha1[ck] = aid
            resp.json.return_value = {"id": aid, "status": "created"}
        elif url.endswith("/albums"):
            name = json["albumName"]
            alb_id = f"alb-{next_album_num}"
            next_album_num += 1
            albums_by_name[name] = alb_id
            album_assets[alb_id] = set()
            resp.json.return_value = {"id": alb_id, "albumName": name}
        return resp

    def fake_put(url: str, headers=None, json=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        alb_id = url.split("/albums/")[1].split("/assets")[0]
        album_assets.setdefault(alb_id, set()).update(json["ids"])
        return resp

    mock_http = MagicMock()
    mock_http.get.side_effect = fake_get
    mock_http.post.side_effect = fake_post
    mock_http.put.side_effect = fake_put

    source = LocalProvider(root_dir=root, albums_only=True)
    target = ImmichProvider(base_url="http://localhost:2283", api_key="test-key", http_client=mock_http)
    syncer = PhotoSynchronizer(dry_run=False, on_progress=lambda _: None)

    # Run 1: uploads `shared.jpg` and `solo.jpg` once, links `shared.jpg` into AlbumB without re-uploading
    res1 = syncer.sync(source, target)
    assert len(res1.copied) == 2
    assert len(res1.albums_updated) == 1
    assert len(res1.errors) == 0
    assert set(albums_by_name.keys()) == {"AlbumA", "AlbumB"}
    assert len(album_assets[albums_by_name["AlbumA"]]) == 2
    assert len(album_assets[albums_by_name["AlbumB"]]) == 1

    # Run 2 (resume after completion with fresh provider instances): skips everything with 0 uploads!
    target2 = ImmichProvider(base_url="http://localhost:2283", api_key="test-key", http_client=mock_http)
    res2 = syncer.sync(source, target2)
    assert len(res2.copied) == 0
    assert len(res2.albums_updated) == 0
    assert len(res2.skipped) == 3
    assert len(res2.errors) == 0

    # Run 3 (simulate interruption before AlbumB was linked): links AlbumB without re-uploading!
    album_assets[albums_by_name["AlbumB"]].clear()
    target3 = ImmichProvider(base_url="http://localhost:2283", api_key="test-key", http_client=mock_http)
    res3 = syncer.sync(source, target3)
    assert len(res3.copied) == 0
    assert len(res3.albums_updated) == 1
    assert len(res3.skipped) == 2
    assert len(album_assets[albums_by_name["AlbumB"]]) == 1


def _make_fake_youtube_service():
    """Create a stateful mock YouTube Data API v3 service for unit tests."""
    videos_store: list[dict] = []
    playlists_store: dict[str, dict] = {}
    playlist_items_store: dict[str, list[str]] = {}
    next_vid = 1
    next_pl = 1

    class _Exec:
        def __init__(self, result):
            self._result = result

        def execute(self):
            return self._result

    class _ResumableInsert:
        def __init__(self, body):
            self._body = body
            self._done = False

        def next_chunk(self):
            nonlocal next_vid
            vid = f"yt-vid-{next_vid}"
            next_vid += 1
            videos_store.append(
                {
                    "id": vid,
                    "snippet": {
                        "title": self._body["snippet"]["title"],
                        "description": self._body["snippet"]["description"],
                        "publishedAt": "2026-01-01T12:00:00Z",
                        "resourceId": {"videoId": vid},
                    },
                    "contentDetails": {"videoId": vid, "videoPublishedAt": "2026-01-01T12:00:00Z"},
                }
            )
            return None, {"id": vid, "snippet": self._body["snippet"]}

    service = MagicMock()

    def channels_list(**kwargs):
        return _Exec({"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU-uploads"}}}]})

    def playlists_list(**kwargs):
        return _Exec({"items": list(playlists_store.values())})

    def playlists_insert(part, body):
        nonlocal next_pl
        pl_id = f"PL-{next_pl}"
        next_pl += 1
        playlists_store[pl_id] = {"id": pl_id, "snippet": body["snippet"]}
        playlist_items_store[pl_id] = []
        return _Exec({"id": pl_id, "snippet": body["snippet"]})

    def playlist_items_list(part, playlistId, maxResults=50, pageToken=None):
        if playlistId == "UU-uploads":
            return _Exec({"items": list(videos_store)})
        items = [
            {"contentDetails": {"videoId": vid}} for vid in playlist_items_store.get(playlistId, [])
        ]
        return _Exec({"items": items})

    def playlist_items_insert(part, body):
        pl_id = body["snippet"]["playlistId"]
        vid = body["snippet"]["resourceId"]["videoId"]
        playlist_items_store.setdefault(pl_id, []).append(vid)
        return _Exec({"id": f"pli-{pl_id}-{vid}"})

    def videos_insert(part, body, media_body):
        return _ResumableInsert(body)

    service.channels.return_value.list.side_effect = channels_list
    service.playlists.return_value.list.side_effect = playlists_list
    service.playlists.return_value.insert.side_effect = playlists_insert
    service.playlistItems.return_value.list.side_effect = playlist_items_list
    service.playlistItems.return_value.insert.side_effect = playlist_items_insert
    service.videos.return_value.insert.side_effect = videos_insert

    return service, videos_store, playlists_store, playlist_items_store


def test_max_video_size_skip_and_youtube_routing(tmp_path: Path) -> None:
    """Verify max_video_size_mb skips large videos on Immich and can route them to YouTube playlists."""
    import os
    from crosspix.youtube import YouTubeProvider

    root = tmp_path / "Ente"
    alb = root / "albums" / "Vacances" / "2025"
    alb.mkdir(parents=True)

    photo = _create_sample_jpeg(alb / "pic.jpg", "2025:07:10 10:00:00", (100, 120, 140))
    small_vid = alb / "small.mp4"
    small_vid.write_bytes(b"\x00" * 50_000)  # ~0.05 MB
    big_vid = alb / "big.mp4"
    big_vid.write_bytes(b"\x01" * (2 * 1024 * 1024))  # 2.0 MB

    # Mock Immich HTTP client accepting everything
    immich_uploads: list[str] = []
    mock_http = MagicMock()
    def fake_get(url, headers=None, params=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = [] if url.endswith("/albums") else {"assets": []}
        return resp
    def fake_post(url, headers=None, json=None, data=None, files=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/assets/bulk-upload-check"):
            resp.json.return_value = {"results": [{"id": e["id"], "action": "accept"} for e in json["assets"]]}
        elif url.endswith("/assets"):
            immich_uploads.append(files["assetData"][0])
            resp.json.return_value = {"id": f"im-{len(immich_uploads)}", "status": "created"}
        elif url.endswith("/albums"):
            resp.json.return_value = {"id": "alb-1", "albumName": json["albumName"]}
        return resp
    def fake_put(url, headers=None, json=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp
    mock_http.get.side_effect = fake_get
    mock_http.post.side_effect = fake_post
    mock_http.put.side_effect = fake_put

    source = LocalProvider(root_dir=root, albums_only=True)
    immich = ImmichProvider(base_url="http://localhost:2283", api_key="k", http_client=mock_http)

    # 1. Skip large videos (> 1 MB) without YouTube routing
    sync_skip = PhotoSynchronizer(max_video_size_mb=1.0, on_progress=lambda _: None)
    res_skip = sync_skip.sync(source, immich)
    assert sorted(immich_uploads) == ["pic.jpg", "small.mp4"]
    assert len(res_skip.skipped_large_videos) == 1
    assert res_skip.skipped_large_videos[0].filename == "big.mp4"

    # 2. Route large videos (> 1 MB) to YouTube automatically during Immich sync
    immich_uploads.clear()
    yt_service, yt_videos, yt_playlists, yt_pl_items = _make_fake_youtube_service()
    yt = YouTubeProvider(service=yt_service, privacy_status="private", on_progress=lambda _: None)
    immich2 = ImmichProvider(base_url="http://localhost:2283", api_key="k", http_client=mock_http)

    sync_route = PhotoSynchronizer(
        max_video_size_mb=1.0,
        large_video_target=yt,
        on_progress=lambda _: None,
    )
    res_route = sync_route.sync(source, immich2)
    assert sorted(immich_uploads) == ["pic.jpg", "small.mp4"]
    assert len(res_route.routed_to_youtube) == 1
    assert len(yt_videos) == 1
    assert "crosspix_sha1:" in yt_videos[0]["snippet"]["description"]
    assert any(pl["snippet"]["title"] == "Vacances 2025" for pl in yt_playlists.values())
    pl_id = next(iter(yt_playlists.keys()))
    assert yt_pl_items[pl_id] == [yt_videos[0]["id"]]

    # 3. Direct streaming sync local -> YouTube with min_video_size_mb=1.0 resumes cleanly without re-uploading!
    alb2 = root / "albums" / "BestOf"
    alb2.mkdir(parents=True)
    os.link(big_vid, alb2 / "big.mp4")

    yt2 = YouTubeProvider(service=yt_service, privacy_status="private", on_progress=lambda _: None)
    sync_yt = PhotoSynchronizer(min_video_size_mb=1.0, on_progress=lambda _: None)
    res_yt = sync_yt.sync(source, yt2)
    # `big.mp4` already uploaded in step 2 -> 0 new uploads, skipped in Vacances 2025, linked in BestOf!
    assert len(res_yt.copied) == 0
    assert len(res_yt.skipped) == 1
    assert len(res_yt.albums_updated) == 1
    assert len(yt_videos) == 1
    assert {pl["snippet"]["title"] for pl in yt_playlists.values()} == {"Vacances 2025", "BestOf"}


def test_no_albums_images_only_and_no_recursive_options(tmp_path: Path) -> None:
    """Verify --no-albums, --images-only, and --no-recursive options across streaming and generic sync."""
    src_root = tmp_path / "src"
    sub_dir = src_root / "Vacances 2025"
    sub_dir.mkdir(parents=True)

    root_img = _create_sample_jpeg(src_root / "root.jpg", "2025:05:01 10:00:00", (10, 20, 30))
    sub_img = _create_sample_jpeg(sub_dir / "sub.jpg", "2025:05:02 11:00:00", (40, 50, 60))
    sub_vid = sub_dir / "clip.mp4"
    sub_vid.write_bytes(b"\x00" * 1024)

    # 1. Streaming local -> Immich with sync_albums=False and images_only=True
    immich_uploads: list[str] = []
    albums_api_calls: list[str] = []
    mock_http = MagicMock()

    def fake_get(url, headers=None, params=None):
        if "/albums" in url:
            albums_api_calls.append(f"GET {url}")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = []
        return resp

    def fake_post(url, headers=None, json=None, data=None, files=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/assets/bulk-upload-check"):
            resp.json.return_value = {
                "results": [{"id": e["id"], "action": "accept"} for e in json["assets"]]
            }
        elif url.endswith("/assets"):
            immich_uploads.append(files["assetData"][0])
            resp.json.return_value = {"id": f"im-{len(immich_uploads)}", "status": "created"}
        elif "/albums" in url:
            albums_api_calls.append(f"POST {url}")
            resp.json.return_value = {"id": "alb-x"}
        return resp

    def fake_put(url, headers=None, json=None):
        albums_api_calls.append(f"PUT {url}")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    mock_http.get.side_effect = fake_get
    mock_http.post.side_effect = fake_post
    mock_http.put.side_effect = fake_put

    source_rec = LocalProvider(root_dir=src_root, recursive=True)
    immich = ImmichProvider(base_url="http://localhost:2283", api_key="k", http_client=mock_http)
    sync_flat_imgs = PhotoSynchronizer(
        sync_albums=False, images_only=True, on_progress=lambda _: None
    )
    res = sync_flat_imgs.sync(source_rec, immich)
    assert sorted(immich_uploads) == ["root.jpg", "sub.jpg"]
    assert albums_api_calls == []
    assert len(res.albums_updated) == 0

    # 2. --no-recursive: only root.jpg is scanned, sub_dir is ignored
    immich_uploads.clear()
    source_nonrec = LocalProvider(root_dir=src_root, recursive=False)
    immich2 = ImmichProvider(base_url="http://localhost:2283", api_key="k", http_client=mock_http)
    res_nr = sync_flat_imgs.sync(source_nonrec, immich2)
    assert immich_uploads == ["root.jpg"]
    assert len(res_nr.copied) == 1

    # 3. Generic local -> local with sync_albums=False copies to _library/ without creating albums/
    dst_root = tmp_path / "dst"
    dst_local = LocalProvider(root_dir=dst_root)
    res_loc = sync_flat_imgs.sync(source_rec, dst_local)
    assert len(res_loc.copied) == 2
    assert (dst_root / "_library" / "2025" / "05" / "root.jpg").exists()
    assert (dst_root / "_library" / "2025" / "05" / "sub.jpg").exists()
    assert not (dst_root / "albums").exists()


def test_sidecar_name_too_long_and_error_log_continuation(tmp_path: Path, monkeypatch) -> None:
    """Verify ENAMETOOLONG on sidecar lookup doesn't crash, and upload errors log to error.log and continue."""
    import errno

    src_root = tmp_path / "src"
    src_root.mkdir()
    img1 = _create_sample_jpeg(src_root / "a_bad.jpg", "2025:05:01 10:00:00", (10, 20, 30))
    img2 = _create_sample_jpeg(src_root / "b_good.jpg", "2025:05:02 11:00:00", (40, 50, 60))

    # 1. Simulate eCryptfs ENAMETOOLONG when checking `.supplemental-metadata.json` sidecar
    orig_is_file = Path.is_file

    def fake_is_file(self: Path) -> bool:
        if self.name.endswith(".supplemental-metadata.json"):
            raise OSError(errno.ENAMETOOLONG, "File name too long", str(self))
        return orig_is_file(self)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    # Create an image with no EXIF so extract_image_metadata reaches sidecar lookup
    from PIL import Image
    no_exif = src_root / "c_long_noexif.jpg"
    Image.new("RGB", (16, 16), (90, 90, 90)).save(no_exif, format="JPEG")
    w, h, dt, _ = LocalProvider.extract_image_metadata(no_exif)
    assert (w, h) == (16, 16)
    assert dt is not None

    # 2. Simulate upload failure on a_bad.jpg while b_good.jpg and c_long_noexif.jpg succeed
    uploaded_names: list[str] = []
    mock_http = MagicMock()

    def fake_post(url, headers=None, json=None, data=None, files=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/assets/bulk-upload-check"):
            resp.json.return_value = {
                "results": [{"id": e["id"], "action": "accept"} for e in json["assets"]]
            }
        elif url.endswith("/assets"):
            fname = files["assetData"][0]
            if fname == "a_bad.jpg":
                raise RuntimeError("Simulated Immich 500 error")
            uploaded_names.append(fname)
            resp.json.return_value = {"id": f"im-{len(uploaded_names)}", "status": "created"}
        return resp

    mock_http.post.side_effect = fake_post

    err_log = tmp_path / "error.log"
    source = LocalProvider(root_dir=src_root)
    immich = ImmichProvider(base_url="http://localhost:2283", api_key="k", http_client=mock_http)
    syncer = PhotoSynchronizer(
        sync_albums=False,
        error_log_path=err_log,
        on_progress=lambda _: None,
    )
    res = syncer.sync(source, immich)

    assert sorted(uploaded_names) == ["b_good.jpg", "c_long_noexif.jpg"]
    assert len(res.copied) == 2
    assert len(res.errors) == 1
    assert err_log.exists()
    log_text = err_log.read_text(encoding="utf-8")
    assert "a_bad.jpg" in log_text
    assert "Simulated Immich 500 error" in log_text


def test_youtube_ssl_eof_retry_and_error_log(tmp_path: Path, monkeypatch) -> None:
    """Verify YouTubeProvider retries transient SSLEOFError automatically and logs persistent errors without aborting."""
    import ssl
    from crosspix.youtube import YouTubeProvider

    monkeypatch.setattr("crosspix.youtube.time.sleep", lambda _: None)

    yt_service, yt_videos, yt_playlists, _ = _make_fake_youtube_service()

    # Inject a transient SSLEOFError on the first videos().insert(...).next_chunk() call
    orig_videos_insert = yt_service.videos.return_value.insert.side_effect
    call_state = {"ssl_failed_once": False}

    def flaky_videos_insert(part, body, media_body):
        inner = orig_videos_insert(part, body, media_body)

        class _FlakyInsert:
            resumable_uri = "https://upload.example/session-1"

            def next_chunk(self):
                if not call_state["ssl_failed_once"]:
                    call_state["ssl_failed_once"] = True
                    raise ssl.SSLEOFError("EOF occurred in violation of protocol (_ssl.c:2417)")
                return inner.next_chunk()

        return _FlakyInsert()

    yt_service.videos.return_value.insert.side_effect = flaky_videos_insert

    root = tmp_path / "Ente"
    alb = root / "albums" / "Famille"
    alb.mkdir(parents=True)
    vid = alb / "clip.mp4"
    vid.write_bytes(b"\x02" * 200_000)

    source = LocalProvider(root_dir=root, albums_only=True)
    yt = YouTubeProvider(service=yt_service, on_progress=lambda _: None)
    err_log = tmp_path / "error.log"
    syncer = PhotoSynchronizer(error_log_path=err_log, on_progress=lambda _: None)

    res = syncer.sync(source, yt)
    assert call_state["ssl_failed_once"] is True
    assert len(res.copied) == 1
    assert len(res.errors) == 0
    assert len(yt_videos) == 1


def test_fix_dates_from_filenames_local_and_immich(tmp_path: Path) -> None:
    """Verify filename date parsing (including PXL_...ms and date-only -> 12:00:00), local mtime, and Immich update."""
    from datetime import datetime
    import os
    from scripts.fix_dates_from_filenames import (
        extract_datetime_from_filename,
        fix_dates_from_filenames,
    )

    # 1. Parsing checks
    assert extract_datetime_from_filename("PXL_20260821_082337164.mp4") == datetime(2026, 8, 21, 8, 23, 37)
    assert extract_datetime_from_filename("20150702_201509_18738559163_o.jpg") == datetime(2015, 7, 2, 20, 15, 9)
    assert extract_datetime_from_filename("IMG-20160221-WA0012.jpeg") == datetime(2016, 2, 21, 12, 0, 0)
    assert extract_datetime_from_filename("Screenshot_2014-12-18-21-19-21.png") == datetime(2014, 12, 18, 21, 19, 21)
    assert extract_datetime_from_filename("20160221_14735170026_o.jpg") == datetime(2016, 2, 21, 12, 0, 0)
    assert extract_datetime_from_filename("daubensee-gemmi_314066442_o.jpg") is None

    # 2. Local mtime + Immich dateTimeOriginal updates
    lib = tmp_path / "_library" / "2026" / "08"
    lib.mkdir(parents=True)
    pxl = lib / "PXL_20260821_082337164.mp4"
    pxl.write_bytes(b"video-bytes-pxl")
    wa = lib / "IMG-20160221-WA0012.jpg"
    wa.write_bytes(b"wa-bytes")
    nodate = lib / "vacances.jpg"
    nodate.write_bytes(b"no-date-bytes")

    # Set wrong mtimes initially
    wrong_ts = datetime(2020, 1, 1, 0, 0, 0).timestamp()
    os.utime(pxl, (wrong_ts, wrong_ts))
    os.utime(wa, (wrong_ts, wrong_ts))

    pxl_sha1 = LocalProvider.compute_sha1_hex(pxl)
    wa_sha1 = LocalProvider.compute_sha1_hex(wa)

    # Mock Immich: pxl exists with wrong date, wa is NOT in Immich
    immich_dates = {"asset-pxl": "2020-01-01T00:00:00Z"}
    updated_calls: list[tuple[str, str]] = []

    mock_http = MagicMock()

    def fake_post(url, headers=None, json=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        results = []
        for entry in json["assets"]:
            if entry["checksum"] == pxl_sha1:
                results.append({"id": entry["id"], "action": "reject", "assetId": "asset-pxl"})
            else:
                results.append({"id": entry["id"], "action": "accept"})
        resp.json.return_value = {"results": results}
        return resp

    def fake_get(url, headers=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        aid = url.rsplit("/assets/", 1)[1]
        resp.json.return_value = {
            "id": aid,
            "fileCreatedAt": immich_dates[aid],
            "exifInfo": {"dateTimeOriginal": immich_dates[aid]},
        }
        return resp

    def fake_put(url, headers=None, json=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        aid = url.rsplit("/assets/", 1)[1]
        immich_dates[aid] = json["dateTimeOriginal"]
        updated_calls.append((aid, json["dateTimeOriginal"]))
        resp.json.return_value = {"id": aid}
        return resp

    mock_http.post.side_effect = fake_post
    mock_http.get.side_effect = fake_get
    mock_http.put.side_effect = fake_put

    immich = ImmichProvider(base_url="http://localhost:2283", api_key="k", http_client=mock_http)

    # Dry-run modifies nothing
    dry = fix_dates_from_filenames(root_dir=tmp_path / "_library", immich=immich, dry_run=True)
    assert dry["dated_files"] == 2
    assert dry["local_updated"] == 2
    assert dry["immich_updated"] == 1
    assert dry["immich_not_found"] == 1
    assert updated_calls == []
    assert abs(pxl.stat().st_mtime - wrong_ts) < 1.0

    # Real run updates both local mtimes and Immich dateTimeOriginal
    stats = fix_dates_from_filenames(root_dir=tmp_path / "_library", immich=immich, dry_run=False)
    assert stats["local_updated"] == 2
    assert stats["immich_updated"] == 1
    assert stats["immich_not_found"] == 1
    assert abs(pxl.stat().st_mtime - datetime(2026, 8, 21, 8, 23, 37).timestamp()) < 1.0
    assert abs(wa.stat().st_mtime - datetime(2016, 2, 21, 12, 0, 0).timestamp()) < 1.0
    assert len(updated_calls) == 1
    assert updated_calls[0][0] == "asset-pxl"
    assert updated_calls[0][1].startswith("2026-08-21T08:23:37")

    # Re-running is idempotent (0 updates)
    stats2 = fix_dates_from_filenames(root_dir=tmp_path / "_library", immich=immich, dry_run=False)
    assert stats2["local_updated"] == 0
    assert stats2["local_already_ok"] == 2
    assert stats2["immich_updated"] == 0
    assert stats2["immich_already_ok"] == 1

    # CLI --tolerance only accepts non-negative integers (rejects floats like 2.0)
    import pytest
    from scripts.fix_dates_from_filenames import main as fix_dates_main

    with pytest.raises(SystemExit) as exc_info:
        fix_dates_main([str(tmp_path / "_library"), "--local-only", "--tolerance", "2.0"])
    assert exc_info.value.code == 2
    assert fix_dates_main([str(tmp_path / "_library"), "--local-only", "--tolerance", "2"]) == 0


def test_youtube_uploads_videos_largest_first(tmp_path: Path) -> None:
    """Verify --largest-first uploads videos in descending size order and reuses a persistent video index without rescanning."""
    from crosspix.youtube import YouTubeProvider

    root = tmp_path / "Ente"
    alb_a = root / "albums" / "AlbumA"
    alb_b = root / "albums" / "AlbumB"
    alb_a.mkdir(parents=True)
    alb_b.mkdir(parents=True)

    (alb_a / "small.mp4").write_bytes(b"\x01" * 100_000)
    (alb_b / "huge.mp4").write_bytes(b"\x02" * 500_000)
    (alb_a / "medium.mp4").write_bytes(b"\x03" * 300_000)

    yt_service, yt_videos, _, _ = _make_fake_youtube_service()
    source = LocalProvider(root_dir=root, albums_only=True)
    yt = YouTubeProvider(service=yt_service, on_progress=lambda _: None)
    syncer = PhotoSynchronizer(largest_first=True, on_progress=lambda _: None)

    res = syncer.sync(source, yt)
    assert [item.filename for item in res.copied] == ["huge.mp4", "medium.mp4", "small.mp4"]
    assert len(yt_videos) == 3

    index_file = root / ".crosspix_video_index.json"
    assert index_file.is_file()

    # Second launch must load from the cached index without calling iter_streaming_folders again!
    source_no_scan = LocalProvider(root_dir=root, albums_only=True)
    source_no_scan.iter_streaming_folders = MagicMock(side_effect=AssertionError("should not rescan folder!"))
    yt2 = YouTubeProvider(service=yt_service, on_progress=lambda _: None)
    res2 = syncer.sync(source_no_scan, yt2)
    assert len(res2.copied) == 0
    assert len(res2.skipped) == 3
    assert source_no_scan.iter_streaming_folders.call_count == 0


def test_youtube_stops_immediately_on_http_429(tmp_path: Path) -> None:
    """Verify YouTube export stops immediately on the first HttpError 429 and resumes from index without rescanning."""
    from googleapiclient.errors import HttpError
    from crosspix.youtube import YouTubeProvider

    root = tmp_path / "Ente"
    alb = root / "albums" / "Famille"
    alb.mkdir(parents=True)
    (alb / "v1_huge.mp4").write_bytes(b"\x01" * 500_000)
    (alb / "v2_med.mp4").write_bytes(b"\x02" * 300_000)
    (alb / "v3_small.mp4").write_bytes(b"\x03" * 100_000)

    yt_service, yt_videos, _, _ = _make_fake_youtube_service()
    orig_insert = yt_service.videos.return_value.insert.side_effect
    attempted_titles: list[str] = []
    fail_v2 = {"active": True}

    def insert_with_429(part, body, media_body):
        title = body["snippet"]["title"]
        attempted_titles.append(title)
        if fail_v2["active"] and "v2_med" in title:
            resp = MagicMock()
            resp.status = 429
            resp.reason = "Too Many Requests"
            raise HttpError(resp=resp, content=b"Too Many Requests")
        return orig_insert(part, body, media_body)

    yt_service.videos.return_value.insert.side_effect = insert_with_429

    err_log = tmp_path / "error.log"
    source = LocalProvider(root_dir=root, albums_only=True)
    yt = YouTubeProvider(service=yt_service, on_progress=lambda _: None)
    syncer = PhotoSynchronizer(largest_first=True, error_log_path=err_log, on_progress=lambda _: None)

    res = syncer.sync(source, yt)
    assert len(res.copied) == 1
    assert res.copied[0].filename == "v1_huge.mp4"
    assert len(res.errors) == 1
    assert len(attempted_titles) == 2  # stopped right after v2_med; v3_small never attempted!
    assert err_log.exists() and "429" in err_log.read_text(encoding="utf-8")

    # Resume next day: skips v1_huge from index without rescanning and uploads v2_med then v3_small!
    fail_v2["active"] = False
    source_resume = LocalProvider(root_dir=root, albums_only=True)
    source_resume.iter_streaming_folders = MagicMock(side_effect=AssertionError("should not rescan folder!"))
    yt_resume = YouTubeProvider(service=yt_service, on_progress=lambda _: None)
    res_resume = syncer.sync(source_resume, yt_resume)
    assert [item.filename for item in res_resume.copied] == ["v2_med.mp4", "v3_small.mp4"]
    assert len(res_resume.skipped) == 1
    assert source_resume.iter_streaming_folders.call_count == 0

    # Also verify HttpError 400 uploadLimitExceeded stops immediately
    from crosspix.youtube import is_youtube_rate_limit_error

    resp400 = MagicMock()
    resp400.status = 400
    resp400.reason = "Bad Request"
    err_limit = HttpError(
        resp=resp400,
        content=(
            b'{"error": {"code": 400, "message": "The user has exceeded the number of videos they may upload.", '
            b'"errors": [{"message": "The user has exceeded the number of videos they may upload.", '
            b'"domain": "youtube.video", "reason": "uploadLimitExceeded"}]}}'
        ),
    )
    assert is_youtube_rate_limit_error(err_limit) is True
















