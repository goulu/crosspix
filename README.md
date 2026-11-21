# crosspix

**crosspix** is a modular Python application and library for synchronizing photos across multiple cloud platforms and local storage:

- **Google Photos** (`crosspix/google_photos.py`)
- **Ente Photos** (`crosspix/ente.py`)
- **Immich** (`crosspix/immich.py`)
- **Local Folders** (`crosspix/local.py`)

Each platform is implemented in its own dedicated module using established PyPI packages and adheres to a unified `PhotoProvider` interface, enabling seamless one-way (`sync`) or bi-directional (`bisync`) synchronization between any pair of platforms.

---

## Key Features

- **Any-to-Any Synchronization**: Sync photos between Local Folders, Google Photos, Ente Photos, and Immich in one-way or bi-directional mode.
- **Zero-Duplication Multi-Album Local Storage (`_library` + `albums`)**:
  - Local storage keeps **one single physical copy** of each photo in `_library/YYYY/MM/<filename>` based on its EXIF capture date (`DateTimeOriginal`).
  - Photos belonging to one or multiple albums are linked inside `albums/...` using **hardlinks** (`--link-mode hardlink`, default) or relative **symlinks** (`--link-mode symlink`), consuming **0 extra bytes** on disk even when a photo belongs to several albums.
- **Space-Delimited Album Hierarchy Mapping**:
  - Cloud platforms (**Google Photos**, **Immich**, and **Ente Photos**) use flat album/collection titles, whereas local filesystems support nested directories.
  - `crosspix` bridges both models by splitting and joining album names with a configurable delimiter (default: space `" "`, `--album-delimiter " "`):
    - Remote album `"Vacances 2026 Italie"` $\leftrightarrow$ `albums/Vacances/2026/Italie/<filename>`
    - Remote album `"Famille Eté Favoris"` $\leftrightarrow$ `albums/Famille/Eté/Favoris/<filename>`
- **Single-Upload Multi-Album Sync across All Cloud Providers**:
  - **Local $\rightarrow$ Cloud**: `LocalProvider` groups hardlinked, symlinked, or identical files by inode (`st_ino`) and SHA-256 checksum into a single `PhotoItem` carrying all its `albums`. The target cloud provider (**Google Photos**, **Immich**, or **Ente**) uploads the binary bytes **only once** and attaches the resulting media/asset/file ID to all corresponding remote albums/collections.
  - **Cloud $\rightarrow$ Local**: Cloud providers merge album/collection memberships per photo so each photo is downloaded **only once** to `_library/YYYY/MM/` and linked into every matching `albums/<hierarchy>/` folder.
  - **Incremental Album Updates (`sync_albums`)**: If a photo already exists on the target platform and is later added to new albums on the source platform, `crosspix` updates only the album links/memberships without re-downloading or re-uploading the photo.
- **Dry-Run Mode**: Preview which photos will be transferred, linked to albums, or skipped with `--dry-run` (`-n`).

---

## Multi-Album & Folder Hierarchy Architecture

```text
Photos/
├── _library/
│   └── 2026/
│       └── 08/
│           └── IMG_001.jpg          # Single physical file on disk (1 inode)
└── albums/
    ├── Vacances/
    │   └── 2026/
    │       └── Italie/
    │           └── IMG_001.jpg      # Hardlink (or symlink) -> _library/2026/08/IMG_001.jpg
    │                                # Maps to album: "Vacances 2026 Italie"
    └── Famille/
        └── Eté/
            └── Favoris/
                └── IMG_001.jpg      # Hardlink (or symlink) -> _library/2026/08/IMG_001.jpg
                                     # Maps to album: "Famille Eté Favoris"
```

### How Each Provider Handles Multi-Album Synchronization

| Provider | Module | PyPI Packages | Multi-Album & Deduplication Behavior |
| :--- | :--- | :--- | :--- |
| **Local Folders** | `crosspix/local.py` | `Pillow` (`PIL`), `piexif` | Stores canonical files in `_library/YYYY/MM/`; links into `albums/<part1>/<part2>/` via `os.link` (hardlink) or `os.symlink`; deduplicates by inode & SHA-256 on scan. |
| **Google Photos** | `crosspix/google_photos.py` | `gphotospy`, `google-api-python-client`, `google-auth-oauthlib`, `requests` | Merges album memberships on `list_photos()`; uploads once via `/v1/uploads` + `mediaItems:batchCreate` and attaches extra albums via `albums/{id}:batchAddMediaItems`. |
| **Immich** | `crosspix/immich.py` | `immichpy`, `httpx` | Enriches assets from `search_assets` with album memberships via `get_all_albums`; uploads once via `POST /assets` and attaches to all albums via `PUT /albums/{id}/assets`. |
| **Ente Photos** | `crosspix/ente.py` | `PyNaCl` (`nacl`), `httpx`, `requests` | Deduplicates files across `/collections/v2` by SHA-256 hash; encrypts (`SecretBox`) and uploads once to S3 + `POST /files`, then links extra collections via `POST /collections/add-files` (or mirrors via local `export_dir`). |

---

## Installation

Clone the repository and use the Python virtual environment (`.venv`):

```bash
git clone https://github.com/goulu/crosspix.git
cd crosspix

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## Configuration & Authentication

### 1. Local Folders (`local`)
Point directly to any local directory (`local:/path/to/photos`).
- `--album-delimiter " "` (default: space `" "`): splits remote album names into nested local directories under `albums/` and joins nested folder names back into remote album titles.
- `--link-mode {hardlink,symlink,copy}` (default: `hardlink`): controls how files in `albums/...` reference the canonical file in `_library/YYYY/MM/`.

### 2. Google Photos (`google_photos`)
1. Enable the **Google Photos Library API** in the [Google Cloud Console](https://console.cloud.google.com/).
2. Create an OAuth 2.0 Client ID (Desktop App) and download the credentials JSON file as `gphoto_oauth.json` (or pass `--google-secret /path/to/oauth.json`).
3. Alternatively, export an active OAuth token via `GOOGLE_PHOTOS_TOKEN`.

### 3. Ente Photos (`ente`)
`EnteProvider` supports two workflows:
- **Direct Ente Museum API (End-to-End Encrypted)**: Uses `PyNaCl` (`nacl.secret.SecretBox`) for client-side encryption/decryption and `httpx`/`requests` to interact with `https://api.ente.io` (or a self-hosted Museum server). Pass `--ente-token` or set `ENTE_AUTH_TOKEN`.
- **Ente CLI Export Directory**: Point `--ente-export-dir` (or `ente:/path/to/ente-export`) to a local directory synchronized by the official `ente-cli`. It uses the same `_library/YYYY/MM/` + `albums/` hardlink/symlink layout.

### 4. Immich (`immich`)
Provide your Immich server API URL (default: `http://localhost:2283/api`) and API key via `--immich-url` and `--immich-key` (or `IMMICH_URL` and `IMMICH_API_KEY` environment variables). Uses `immichpy.AsyncClient` and `httpx`.

---

## Command-Line Usage

### List Photos and Album Memberships

```bash
# List deduplicated photos and all their album memberships in a local directory
crosspix list local:~/Pictures/Photos

# List photos and albums on an Immich server
crosspix list immich --immich-url http://localhost:2283/api --immich-key YOUR_API_KEY

# List photos in a specific Google Photos album
crosspix list google_photos --google-secret gphoto_oauth.json --album "Vacances 2026 Italie"
```

### Synchronize Photos Between Platforms

```bash
# Sync Google Photos (multi-album) to local folder hierarchy using hardlinks
crosspix sync google_photos local:~/Pictures/Photos --link-mode hardlink

# Sync Immich to local folder hierarchy using relative symlinks
crosspix sync immich local:~/Pictures/Photos --immich-key YOUR_API_KEY --link-mode symlink

# Sync local folder hierarchy to Immich (single upload per photo, attached to all matching albums)
crosspix sync local:~/Pictures/Photos immich --immich-key YOUR_API_KEY

# Sync local folder hierarchy to Ente Photos
crosspix sync local:~/Pictures/Photos ente --ente-token YOUR_ENTE_TOKEN

# Bi-directional sync between Google Photos and Immich
crosspix sync google_photos immich --immich-key YOUR_API_KEY --bidirectional

# Dry-run preview
crosspix sync google_photos local:~/Pictures/Photos --dry-run
```

---

## Python API Usage

```python
from crosspix import (
    LocalProvider,
    GooglePhotosProvider,
    EnteProvider,
    ImmichProvider,
    PhotoSynchronizer,
)

# LocalProvider with _library/YYYY/MM/ + space-delimited albums/ hierarchy
local = LocalProvider(
    "~/Pictures/Photos",
    album_delimiter=" ",
    link_mode="hardlink",  # "hardlink", "symlink", or "copy"
)

immich = ImmichProvider(
    base_url="http://localhost:2283/api",
    api_key="your-immich-api-key",
)

# Synchronize between any two providers (Google Photos, Immich, Ente, Local)
synchronizer = PhotoSynchronizer(dry_run=False, on_progress=print)
result = synchronizer.sync(source=immich, target=local)

print(
    f"Copied: {len(result.copied)}, "
    f"Album links updated: {len(result.albums_updated)}, "
    f"Skipped: {len(result.skipped)}"
)
```

---

## Running Tests

```bash
.venv/bin/pytest -v
```

---

## License

MIT License
