# crosspix

**crosspix** is a modular Python application and library for synchronizing photos across multiple cloud platforms and local storage:

- **Google Photos** (`crosspix/google_photos.py`)
- **Ente Photos** (`crosspix/ente.py`)
- **Immich** (`crosspix/immich.py`)
- **YouTube** (`crosspix/youtube.py`)
- **Local Folders** (`crosspix/local.py`)

Each platform is implemented in its own dedicated module using established PyPI packages and adheres to a unified `PhotoProvider` interface, enabling seamless one-way (`sync`) or bi-directional (`bisync`) synchronization between any pair of platforms into a single shared local directory without duplicating files.

---

## Key Features

- **Any-to-Any Synchronization into a Shared Target**: Synchronize multiple sources (e.g. **Google Photos**, **Ente Photos**, and **Immich**) into the exact same local directory (`/home/user/Images/Photos`). Photos already present from one source are never re-downloaded from another.
- **Zero-Duplication Multi-Album Local Storage (`_library` + `albums`)**:
  - Local storage keeps **one single physical copy** of each photo in `_library/YYYY/MM/<filename>` based on its EXIF capture date (`DateTimeOriginal`) or sidecar JSON metadata.
  - Photos belonging to one or multiple albums are linked inside `albums/...` using **hardlinks** (`--link-mode hardlink`, default) or relative **symlinks** (`--link-mode symlink`), consuming **0 extra bytes** on disk even when a photo belongs to several albums.
- **Space-Delimited Album Hierarchy Mapping**:
  - Cloud platforms (**Google Photos**, **Immich**, and **Ente Photos**) use flat album/collection titles, whereas local filesystems support nested directories.
  - `crosspix` bridges both models by splitting and joining album names with a configurable delimiter (default: space `" "`, `--album-delimiter " "`):
    - Remote album `"Vacances 2026 Italie"` $\leftrightarrow$ `albums/Vacances/2026/Italie/<filename>`
    - Remote album `"Zermatt 2015"` $\leftrightarrow$ `albums/Zermatt/2015/<filename>`
- **Selective Download & Multi-Hash Deduplication (`SHA-256` + `SHA-512`)**:
  - `LocalProvider` computes both `SHA-256` (hex) and `SHA-512` (Base64, matching Ente's native metadata hash) in a single pass, alongside EXIF timestamps and file sizes.
  - When syncing from **Ente Photos** or **Immich** into a non-empty local directory, `crosspix` compares remote metadata against local files first:
    - **If a photo already exists locally** (e.g. previously synced from Google Photos), it is **not downloaded again**; `crosspix` only creates any missing album hardlinks under `albums/`.
    - **Only missing photos** are downloaded individually.
- **Real-Time Progress Output**: Unbuffered step-by-step console logging shows authentication, album scanning, `[i/N]` download progress, canonical storage, and hardlink creation in real time.

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

### How Each Provider Works

| Provider | Module | PyPI Packages | Synchronization & Multi-Album Behavior |
| :--- | :--- | :--- | :--- |
| **Local Folders** | `crosspix/local.py` | `Pillow` (`PIL`), `piexif` | Stores canonical files in `_library/YYYY/MM/`; links into `albums/<part1>/<part2>/` via `os.link` (hardlink) or `os.symlink`; supports `--albums-only` to scan only `albums/` (skipping `_library/`, `.meta/`, and `metadata/`); deduplicates by inode, `SHA-256`, `SHA-512` (Base64), and `SHA-1` (Base64); reads EXIF and Ente/Takeout sidecar JSON timestamps. |
| **Google Photos** | `crosspix/google_photos.py` | `gphotospy`, `google-api-python-client`, `google-auth-oauthlib`, `requests` | Supports OAuth2 Library API + automatic fallback to the **Google Photos Picker API** (`photospicker.googleapis.com` for post-March 2025 scopes), plus full automatic import of photos & albums from a **Google Takeout** directory (`--google-takeout`). |
| **Ente Photos** | `crosspix/ente.py` | `PyNaCl` (`nacl`), `httpx`, `requests` | **Selective CLI mode (`ente-cli v0.4.5+`)**: fetches album/file metadata & `SHA-512` hashes via `ente photos album list --all --json` and `ente photos file list --all --json --offline`, and downloads **only missing files** via `ente photos file download`. Also supports local export folders and the E2EE Museum API. |
| **Immich** | `crosspix/immich.py` | `immichpy`, `httpx` | Streams local albums directly via `POST /assets/bulk-upload-check` (resumable, no upfront full scan); uploads once via `POST /assets` with `x-immich-checksum` and attaches to albums via `PUT /albums/{id}/assets`. |
| **YouTube** | `crosspix/youtube.py` | `google-api-python-client`, `google-auth-oauthlib` | Resumable chunked video uploads via YouTube Data API v3 (`private` by default); maps crosspix albums to YouTube playlists; embeds `crosspix_sha1` in descriptions for deduplication and resumability. |

---

## Installation

`crosspix` requires **Python 3.11 or newer** (Python 3.12 recommended). Below are complete step-by-step instructions for **Windows**, **macOS**, and **Linux** — even if Python and Git are not yet installed on your machine.

### Windows (10 / 11)

1. **Install Python and Git** (open **PowerShell** from the Start menu):
   ```powershell
   winget install -e --id Python.Python.3.12
   winget install -e --id Git.Git
   ```
   *(Alternatively, download the official installers from [python.org/downloads](https://www.python.org/downloads/) — make sure to check **"Add python.exe to PATH"** during setup — and [git-scm.com](https://git-scm.com/downloads). Then close and reopen PowerShell.)*
2. **Download `crosspix` and install it in a virtual environment**:
   ```powershell
   git clone https://github.com/goulu/crosspix.git
   cd crosspix

   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   ```
3. **Verify the installation**:
   ```powershell
   .\.venv\Scripts\python.exe -m crosspix --help
   ```
   *(On Windows, replace `.venv/bin/python` in the examples below with `.\.venv\Scripts\python.exe`.)*

### macOS

1. **Install Python and Git** (open **Terminal**):
   - **Option A — via [Homebrew](https://brew.sh/)** (recommended):
     ```bash
     brew install python@3.12 git
     ```
   - **Option B — without Homebrew**: download the macOS installer from [python.org/downloads](https://www.python.org/downloads/) and run `xcode-select --install` in Terminal to install Git.
2. **Download `crosspix` and install it in a virtual environment**:
   ```bash
   git clone https://github.com/goulu/crosspix.git
   cd crosspix

   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   pip install -e ".[dev]"
   ```
3. **Verify the installation**:
   ```bash
   .venv/bin/python -m crosspix --help
   ```

### Linux (Ubuntu / Debian / Linux Mint / Fedora / Arch)

1. **Install Python, `venv`, and Git**:
   ```bash
   # Ubuntu / Debian / Linux Mint
   sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

   # Fedora
   sudo dnf install -y python3 python3-pip git

   # Arch Linux
   sudo pacman -S python python-pip git
   ```
2. **Download `crosspix` and install it in a virtual environment**:
   ```bash
   git clone https://github.com/goulu/crosspix.git
   cd crosspix

   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   pip install -e ".[dev]"
   ```
3. **Verify the installation**:
   ```bash
   .venv/bin/python -m crosspix --help
   ```

> **Tip (no Git?)**: On any OS, you can also click **Code → Download ZIP** on [github.com/goulu/crosspix](https://github.com/goulu/crosspix), extract the archive, open a terminal inside the extracted folder, and run steps 2–3 starting from the `venv` creation command.

---

## Configuration & Authentication by Platform

### 1. Local Folders (`local:/path/to/photos`)
- `--album-delimiter " "` (default: space `" "`): splits remote album names into nested local directories under `albums/` and joins nested folder names back into remote album titles.
- `--link-mode {hardlink,symlink,copy}` (default: `hardlink`): controls how files in `albums/...` reference the canonical file in `_library/YYYY/MM/`.
- `--albums-only`: when reading from a local source, scans only `albums/` (ignoring unlinked files in `_library/`, hidden directories such as `.meta/`, and `metadata/`). Hardlinked photos shared across multiple albums are still uploaded once and linked to all their albums.
- `--no-albums`: synchronizes files only without creating or linking albums/folders/playlists on the target (useful e.g. when uploading `_library/` or flat folders to Immich without turning subdirectories into albums).
- `--images-only`: synchronizes image files only and skips all videos.
- `--no-recursive`: scans only the specified local directory without descending into subdirectories.

---

### 2. Google Photos (`google_photos`)

#### A. OAuth 2.0 & Google Photos Picker API
> **Note on Google's March 31, 2025 API Changes**: Google deprecated the broad `photoslibrary.readonly` scope for reading existing non-app-created photos. When `crosspix` detects `HTTP 403` on the legacy Library API, it automatically switches to the **Google Photos Picker API** (`photospicker.mediaitems.readonly`), opens the Google Photos Picker selection window in your browser, and downloads the selected photos.

1. In the [Google Cloud Console](https://console.cloud.google.com/), enable both **Google Photos Library API** and **Google Photos Picker API**.
2. Under **OAuth consent screen / Audience**, add your Google email address to **Test users**.
3. Create an OAuth 2.0 Client ID (**Desktop App**) and save the JSON file as `gphoto_oauth.json` in the project root.
4. Synchronize:
   ```bash
   .venv/bin/python -m crosspix sync google_photos local:/home/goulu/Images/Photos --link-mode hardlink
   ```

#### B. Automatic Full Library + All Albums Import via Google Takeout
To import 100% of your existing Google Photos library and 100% of your albums automatically without manual Picker selection:
1. Export your **Google Photos** archive from [takeout.google.com](https://takeout.google.com/) and extract it.
2. Run:
   ```bash
   .venv/bin/python -m crosspix sync google_photos local:/home/goulu/Images/Photos --google-takeout /path/to/Takeout --link-mode hardlink
   ```
   `crosspix` deduplicates every photo exported across multiple Takeout album folders into a single physical file in `_library/YYYY/MM/` and creates hardlinks in `albums/<word1>/<word2>/`.

---

### 3. Ente Photos (`ente`)

#### Selective Synchronization with `ente-cli v0.4.5+` (Recommended)
Unlike `ente photos export` (which refuses to export into a non-empty folder shared with other photo sources), `crosspix` queries `ente-cli` metadata in JSON and selectively downloads **only the photos not already present** in your target folder.

1. **Install `ente-cli` (`v0.4.5+`) on Linux (`x86_64`)**:
   ```bash
   mkdir -p ~/.local/bin
   curl -fL "https://github.com/ente/ente/releases/download/ente-cli-v0.4.5/ente-cli-v0.4.5-linux-amd64.tar.gz" \
     | tar -xz -C ~/.local/bin
   chmod +x ~/.local/bin/ente
   ```
2. **Log in to your Ente account once**:
   ```bash
   ~/.local/bin/ente account login --app photos
   ```
   *(If no account is logged in yet, `crosspix` will automatically prompt `ente account login --app photos` on first run).*
3. **Synchronize all photos and albums selectively into `/home/goulu/Images/Photos`**:
   ```bash
   .venv/bin/python -m crosspix sync ente local:/home/goulu/Images/Photos --link-mode hardlink
   ```
   - To sync only a specific Ente album:
     ```bash
     .venv/bin/python -m crosspix sync ente local:/home/goulu/Images/Photos --album "Zermatt 2015" --link-mode hardlink
     ```
   - To preview without downloading (`--dry-run`):
     ```bash
     .venv/bin/python -m crosspix sync ente local:/home/goulu/Images/Photos --dry-run
     ```

---

### 4. Immich (`immich`)
Provide your Immich server URL (default: `http://localhost:2283/api`; `/api` is appended automatically if omitted) and API key in one of three ways (checked in this order):
1. `--immich-key YOUR_API_KEY`
2. `IMMICH_API_KEY` environment variable
3. A gitignored local token file (`immich.token` by default, or `--immich-key-file PATH`; blank lines and `#` comments are ignored).

```bash
# Sync from Immich to local folder hierarchy
.venv/bin/python -m crosspix sync immich:http://localhost:2283 local:/home/goulu/Images/Photos --link-mode hardlink

# Sync full local library + albums to Immich (reads API key from immich.token)
.venv/bin/python -m crosspix sync local:/home/goulu/Images/Ente immich:http://localhost:2283

# Sync ONLY albums and their photos to Immich (skip unlinked photos in _library/ and videos > 100 MB by default)
.venv/bin/python -m crosspix sync local:/home/goulu/Images/Ente immich:http://localhost:2283 --albums-only

# Sync to Immich without any video size limit (--max-video-size-mb 0)
.venv/bin/python -m crosspix sync local:/home/goulu/Images/Ente immich:http://localhost:2283 --albums-only --max-video-size-mb 0

# Sync to Immich AND automatically upload videos > 100 MB to YouTube playlists in one pass
.venv/bin/python -m crosspix sync local:/home/goulu/Images/Ente immich:http://localhost:2283 --albums-only --max-video-size-mb 100 --youtube-large-videos
```

---

### 5. YouTube (`youtube`) & Large Video Filtering
By default, `crosspix sync` to non-YouTube targets skips video files larger than **100 MB** (`--max-video-size-mb 100`, pass `0` to disable). You can synchronize those large videos (or all videos) directly to YouTube, organized into playlists matching your album names:

1. In [Google Cloud Console](https://console.cloud.google.com/), enable **YouTube Data API v3** on your project (you can reuse `gphoto_oauth.json` or save a dedicated `youtube_oauth.json`, gitignored via `*oauth*.json`).
2. On first run, your browser opens for OAuth consent and caches the token in `youtube.token` (gitignored via `*.token`).
3. Uploaded videos and created playlists default to **`private`** (configurable via `--youtube-privacy {private,unlisted,public}`), and embed `crosspix_sha1` in the description so re-runs skip already-uploaded videos automatically.

```bash
# Sync ONLY videos >= 100 MB from albums/ to private YouTube playlists
.venv/bin/python -m crosspix sync local:/home/goulu/Images/Ente youtube --albums-only --min-video-size-mb 100

# Upload largest videos first (creates & reuses <source>/.crosspix_video_index.json to avoid rescanning; stops immediately on HttpError 429 or uploadLimitExceeded)
.venv/bin/python -m crosspix sync local:/home/goulu/Images/Ente youtube --min-video-size-mb 100 --largest-first

# Preview which large videos would be uploaded to YouTube
.venv/bin/python -m crosspix sync local:/home/goulu/Images/Ente youtube --albums-only --min-video-size-mb 100 --dry-run

# Sync to Immich (photos + videos <= 100 MB) and route videos > 100 MB to YouTube in one command
.venv/bin/python -m crosspix sync local:/home/goulu/Images/Ente immich:http://localhost:2283 --albums-only --youtube-large-videos --youtube-privacy unlisted
```

---

## Maintenance Scripts (`scripts/`)

### 1. Migrate an Ente Export Directory (`scripts/migrate_ente_folder.py`)
Sequentially reorganizes a flat Ente export directory into crosspix's `_library/YYYY/MM/` and hierarchical `albums/` layout without building a global catalog in memory:
- Streams directory-by-directory (`os.walk`) with live terminal progress.
- Deduplicates locally against `_library/YYYY/MM/` (fast inode/size checks before SHA-256) and creates space-delimited album hardlinks.
- Moves remaining non-media files (e.g. `.gpx`, `.txt`) and auxiliary hidden folders (e.g. `.meta`) into the corresponding `albums/...` directory.
- Cleans up processed sidecar JSONs and empty legacy directories as it goes.

```bash
# Preview changes without modifying disk
.venv/bin/python scripts/migrate_ente_folder.py /home/goulu/Images/Ente --dry-run

# Reorganize in-place
.venv/bin/python scripts/migrate_ente_folder.py /home/goulu/Images/Ente
```

### 2. Deduplicate Variant Copies in `_library` (`scripts/deduplicate_library_variants.py`)
Cleans up redundant variant copies within each `_library/YYYY/MM/` folder and repairs all affected links in `albums/` in two fast sequential phases:
- **Supported variants**: Flickr originals (`_<id>_o`), Google Photos edits (`-modifié`, `-edited`), hash-disambiguated copies (`_<8hex>`), and numbered copies (`(1)`, `(2)`).
- **Priority**: keeps `_<id>_o` first, then `-modifié`, then highest resolution (`width × height`) and largest file size, and renames the kept copy to its short base name.
- **Safety**: uses perceptual 64-bit `dHash` clustering for multi-`_o`, `_<8hex>`, and `(N)` groups so visually distinct photos sharing a title or filename are preserved untouched.
- **Phase 2 (`albums/`)**: scans each album folder sequentially via `os.scandir`, updating/rebuilding hardlinks and symlinks (including broken symlinks and orphaned `nlink == 1` hardlinks).

```bash
# Preview without modifying disk
.venv/bin/python scripts/deduplicate_library_variants.py /home/goulu/Images/Ente --dry-run

# Deduplicate _library and rebuild album links
.venv/bin/python scripts/deduplicate_library_variants.py /home/goulu/Images/Ente

# Optional flags: --rename-standalone, --skip-albums, --albums-only, --verbose
```

### 3. Fix Local & Immich Dates from Filenames (`scripts/fix_dates_from_filenames.py`)
Walks a directory sequentially (e.g. `/home/goulu/Images/Ente/_library`, ignoring hidden directories such as `.meta/` and `metadata/`), extracts a capture date/time from each media filename, adjusts the local file's modification time (`mtime`), and updates the matching asset's `dateTimeOriginal` in Immich if present:

- **Supported filename patterns**:
  - Date + time (with optional 3 millisecond digits): `PXL_20260821_082337164.mp4` → `2026-08-21 08:23:37`, `20150702_201509_18738559163_o.jpg` → `2015-07-02 20:15:09`, `IMG_20240520_114346.jpg`, `Screenshot_2014-12-18-21-19-21.png`.
  - Date only (no time present): `IMG-20160221-WA0012.jpeg` or `20160221_14735170026_o.jpg` → defaults time to **`12:00:00`** (`2016-02-21 12:00:00`).
  - Long numeric identifiers (such as Flickr IDs) are safely ignored.
- **Local update**: updates `mtime` via `os.utime` (which automatically updates hardlinked copies under `albums/`) only when it differs by more than `--tolerance` seconds.
- **Immich update**: looks up files in Immich in batches (`POST /api/assets/bulk-upload-check` using SHA-1, reading the API key from `immich.token` by default), compares the asset's current `dateTimeOriginal` (`GET /api/assets/{id}`), and calls `PUT /api/assets/{id}` only when the timestamp differs by more than `--tolerance` seconds.
- **Options**:
  - `--local-only` / `--immich-only`: restrict updates to local files or to Immich only.
  - `--tolerance N`: whole number of seconds (`>= 0`, default: `2`) within which existing dates are considered already accurate (re-running is idempotent).
  - `--immich-url URL`, `--immich-key KEY`, `--immich-key-file PATH` (default: `immich.token`).
  - `--batch-size N` (default: `50`), `--error-log PATH` (default: `error.log`), `--dry-run` / `-n`, `--verbose` / `-v`.

```bash
# Preview changes without modifying disk or Immich
.venv/bin/python scripts/fix_dates_from_filenames.py /home/goulu/Images/Ente/_library --dry-run

# Adjust local mtimes and Immich dates (reads API key from immich.token)
.venv/bin/python scripts/fix_dates_from_filenames.py /home/goulu/Images/Ente/_library

# Adjust local mtimes only, with a 5-second integer tolerance
.venv/bin/python scripts/fix_dates_from_filenames.py /home/goulu/Images/Ente/_library --local-only --tolerance 5 --verbose
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

local = LocalProvider(
    "/home/goulu/Images/Photos",
    album_delimiter=" ",
    link_mode="hardlink",
)
ente = EnteProvider()

synchronizer = PhotoSynchronizer(dry_run=False)
result = synchronizer.sync(source=ente, target=local)

print(
    f"Copied: {len(result.copied)}, "
    f"Album links updated: {len(result.albums_updated)}, "
    f"Skipped: {len(result.skipped)}"
)
```

---

## Running Tests

```bash
.venv/bin/python -m pytest -v
```

---

## License

MIT License
