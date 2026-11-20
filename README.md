# crosspix

**crosspix** is a modular Python application and library for synchronizing photos across multiple cloud platforms and local storage:

- **Google Photos** (`crosspix/google_photos.py`)
- **Ente Photos** (`crosspix/ente.py`)
- **Immich** (`crosspix/immich.py`)
- **Local Folders** (`crosspix/local.py`)

Each platform is implemented in its own dedicated module using established PyPI packages and adheres to a unified `PhotoProvider` interface, enabling seamless one-way (`sync`) or bi-directional (`bisync`) synchronization between any pair of platforms.

---

## Features

- **Any-to-Any Synchronization**: Sync photos between Local Folders, Google Photos, Ente Photos, and Immich in one-way or bi-directional mode.
- **Modular Architecture**: Each platform lives in a separate file implementing the `PhotoProvider` abstract base class (`connect`, `list_photos`, `download_photo`, `upload_photo`).
- **Smart Deduplication**: Matches photos across providers using SHA-256 checksums, filenames, byte sizes, and EXIF capture timestamps (`DateTimeOriginal`) to prevent duplicate uploads.
- **Dry-Run Support**: Preview which photos will be transferred or skipped before modifying any storage target.

---

## Project Structure & PyPI Dependencies

| Module | Platform | PyPI Packages Imported |
| :--- | :--- | :--- |
| `crosspix/local.py` | Local Folders | `Pillow` (`PIL`), `piexif` |
| `crosspix/google_photos.py` | Google Photos | `gphotospy`, `google-api-python-client`, `google-auth-oauthlib`, `requests` |
| `crosspix/ente.py` | Ente Photos | `PyNaCl` (`nacl`), `httpx`, `requests` |
| `crosspix/immich.py` | Immich | `immichpy`, `httpx` |
| `crosspix/sync.py` | Sync Engine | Core synchronization & deduplication logic (`PhotoSynchronizer`) |
| `crosspix/cli.py` | CLI | Command-line interface (`crosspix`) |

---

## Installation

Clone the repository and use the included Python virtual environment (`.venv`):

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
Point directly to any local directory containing photos (`.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, `.tiff`, etc.). `LocalProvider` automatically reads EXIF metadata (`DateTimeOriginal`) and dimensions via `Pillow` and `piexif` and calculates SHA-256 checksums.

### 2. Google Photos (`google_photos`)
1. Enable the **Google Photos Library API** in the [Google Cloud Console](https://console.cloud.google.com/).
2. Create an OAuth 2.0 Client ID (Desktop App) and download the credentials JSON file as `gphoto_oauth.json` (or pass `--google-secret /path/to/oauth.json`).
3. Alternatively, export an active OAuth token via `GOOGLE_PHOTOS_TOKEN`.

### 3. Ente Photos (`ente`)
`EnteProvider` supports two workflows:
- **Direct Ente Museum API (End-to-End Encrypted)**: Uses `PyNaCl` (`nacl.secret.SecretBox`) for client-side encryption/decryption and `httpx`/`requests` to interact with `https://api.ente.io` (or a self-hosted Museum server). Pass `--ente-token` or set `ENTE_AUTH_TOKEN`.
- **Ente CLI Export Directory**: Point `--ente-export-dir` (or `ente:/path/to/ente-export`) to a local directory synchronized by the official `ente-cli`.

### 4. Immich (`immich`)
Provide your Immich server API URL (default: `http://localhost:2283/api`) and API key via `--immich-url` and `--immich-key` (or `IMMICH_URL` and `IMMICH_API_KEY` environment variables). Uses `immichpy.AsyncClient` and `httpx`.

---

## Command-Line Usage

### List Photos on a Provider

```bash
# List photos in a local directory
crosspix list local:~/Pictures/Vacation

# List photos on an Immich server
crosspix list immich --immich-url http://localhost:2283/api --immich-key YOUR_API_KEY

# List photos in a Google Photos album
crosspix list google_photos --google-secret gphoto_oauth.json --album "Vacation"
```

### Synchronize Photos Between Platforms

```bash
# One-way sync from a local directory to Immich
crosspix sync local:~/Pictures/2026 immich --immich-key YOUR_API_KEY

# Bi-directional sync between two local folders
crosspix sync local:~/Pictures/Camera local:~/Backup/Photos --bidirectional

# Dry-run sync from Google Photos to Ente export folder
crosspix sync google_photos ente:~/EnteExport --dry-run

# Sync a specific album from Immich to a local folder
crosspix sync immich local:~/Pictures/ImmichBackup --album "Family" --immich-key YOUR_API_KEY
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

# Initialize any combination of providers
local = LocalProvider("~/Pictures/Photos")
immich = ImmichProvider(
    base_url="http://localhost:2283/api",
    api_key="your-immich-api-key",
)

# Synchronize local photos with Immich
synchronizer = PhotoSynchronizer(dry_run=False, on_progress=print)
result = synchronizer.sync(source=local, target=immich)

print(f"Copied: {len(result.copied)}, Skipped: {len(result.skipped)}")
```

---

## Running Tests

```bash
.venv/bin/pytest -v
```

---

## License

MIT License
