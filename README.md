# Nugs Downloader

Python downloader, FastAPI server, and local web UI for nugs.net downloads.

Original Go project: https://github.com/Sorrow446/Nugs-Downloader

## What This App Does

- Browse the nugs catalog by availability, band, year, and download mode.
- Queue selected shows from the catalog table.
- Download audio only, video only, or both.
- Select audio/video quality and output formats.
- Pause, resume, cancel, and delete queued downloads.
- Track progress, output files, file sizes, and queue state in the web UI.
- Store each user's nugs login in an encrypted local credential file rather than in source-controlled config.
- Run packaged builds in a standalone desktop window instead of opening the default browser.
- Build local macOS and Windows packages without bundling credentials or development files.

## Requirements

For packaged desktop app users:

- macOS 12+ for the `.dmg`, or Windows 10/11 for the `.exe` / portable zip.
- `ffmpeg` available on `PATH`, unless you place an `ffmpeg` binary next to the app runtime.
- Valid nugs.net credentials entered in the app header each time you open the app.

For local command-line or development usage:

- Python 3.10+
- ffmpeg available on `PATH` or present as `./ffmpeg`
- Valid nugs.net credentials entered through the web UI

## Quick Start

### macOS DMG

1. Download or build `dist/NugsDownloader-macOS.dmg`.
2. Double-click the DMG.
3. Double-click `Nugs Downloader.app` in the mounted disk image.
4. Enter your nugs email/password in the header and click `Login`.
5. Choose filters, select shows in `Queue Selection`, then click `Queue Downloads`.
6. Use `Current Downloads` to pick an output folder, start/pause downloads, cancel, delete, refresh, and open completed files.

The macOS app opens in a standalone window. It stores runtime state under:

```text
~/Library/Application Support/NugsDownloader/
```

Credentials are session-only for the packaged app: saved credential files are deleted on app launch and again when the app closes, so you should expect to log in each time.

The packaged macOS default output folder is:

```text
~/Music/Nugs Downloader
```

Click the output path field in `Current Downloads` to choose a different folder with the native folder picker.

### Windows EXE Or Portable Zip

There are two Windows package styles:

- `dist/NugsDownloader-Windows-portable.zip`: portable source/runtime package.
- `Nugs Downloader.exe`: real executable built by GitHub Actions or by running `build-exe.ps1` on Windows.

To use the portable zip:

1. Extract `dist/NugsDownloader-Windows-portable.zip`.
2. Open the extracted `NugsDownloader` folder.
3. Run `run-server.bat`.
4. Use the app window if packaged as an exe, or open `http://127.0.0.1:8090/` if running the portable server.

To build and run the Windows executable locally:

```powershell
Expand-Archive -Path .\dist\NugsDownloader-Windows-portable.zip -DestinationPath .\dist\windows-expanded -Force
Set-Location .\dist\windows-expanded\NugsDownloader
.\build-exe.ps1
& ".\dist\Nugs Downloader.exe"
```

Windows runtime state is stored under:

```text
%LOCALAPPDATA%\NugsDownloader\
```

The packaged Windows default output folder is:

```text
%USERPROFILE%\Music\Nugs Downloader
```

### Local Command Line Or Dev Server

Install dependencies:

```bash
python -m venv p3venv
./p3venv/bin/pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -3 -m venv p3venv
.\p3venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then run either the web app or the CLI as described below.

## Configuration And Credentials

`config.json` is optional. The app can run without it and will use built-in defaults plus encrypted credentials entered through the web UI.

If you want local non-secret defaults, copy `config.example.json` to `config.json`.

```bash
cp config.example.json config.json
```

`config.json` is intentionally gitignored. It should contain app defaults only, not your nugs email or password. It is not required for packaged app users.

Credentials are entered in the web UI header and stored in encrypted local files:

- `nugs_credentials.enc`
- `nugs_credentials.key`

Those files are generated per user, gitignored, and must not be committed or packaged.

| Option | Info |
| --- | --- |
| format | Track download quality. `1=ALAC`, `2=FLAC`, `3=MQA`, `4=360`, `5=AAC`. |
| videoFormat | Video quality. `1=480p`, `2=720p`, `3=1080p`, `4=1440p`, `5=4K`. |
| audioOutputFormat | Final audio output format, such as `source`, `mp3`, or `flac`. |
| videoOutputFormat | Final video container, such as `mkv` or `mp4`. |
| outPath | Output directory. Created automatically if missing. |
| useFfmpegEnvVar | `true` by default to use ffmpeg from `PATH`; set `false` only when using a local `./ffmpeg` binary. |

You can override local state paths with environment variables:

```bash
export NUGS_CONFIG_PATH="$HOME/.config/nugs-downloader/config.json"
export NUGS_CREDENTIALS_PATH="$HOME/.config/nugs-downloader/nugs_credentials.enc"
export NUGS_CREDENTIALS_KEY_PATH="$HOME/.config/nugs-downloader/nugs_credentials.key"
export NUGS_HISTORY_DB_PATH="$HOME/.config/nugs-downloader/download_history.sqlite3"
export NUGS_DEFAULT_OUT_PATH="$HOME/Music/Nugs Downloader"
```

## Run The Web App

Start the API server:

```bash
./p3venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8090
```

Open the web interface:

```text
http://127.0.0.1:8090/
```

Use the UI flow:

1. Enter your nugs email/password in the header and click `Login`.
2. Choose `Availability`, `Download mode`, `Band`, and `Year`.
3. Pick audio/video output and quality options.
4. Select shows in `Queue Selection`.
5. Click `Queue Downloads`.
6. Use `Current Downloads` to start, pause, cancel, delete, refresh, and inspect output.

Downloads start paused by default. Use the play/start button in `Current Downloads` to begin.

## Output Folder

The default local Docker output path is:

```text
data/private_sources/nugs/inbox/
```

The Docker Compose service maps that folder to:

```text
/app/data/private_sources/nugs/inbox
```

Credential files for the Docker service are stored separately under:

```text
data/private_sources/nugs/secrets/
```

## CLI Usage

Arguments override `config.json`.

Show help:

```bash
./p3venv/bin/python main.py --help
```

Download two albums:

```bash
./p3venv/bin/python main.py https://play.nugs.net/release/23329 https://play.nugs.net/release/23790
```

Download video only:

```bash
./p3venv/bin/python main.py -F 5 --force-video https://play.nugs.net/watch/release/38966
```

Download audio only:

```bash
./p3venv/bin/python main.py --skip-videos https://play.nugs.net/release/23329
```

Convert audio output to MP3:

```bash
./p3venv/bin/python main.py --audio-output-format mp3 https://play.nugs.net/release/23329
```

Package video output as MP4:

```bash
./p3venv/bin/python main.py --video-output-format mp4 --force-video https://play.nugs.net/watch/release/38966
```

## REST API

Main endpoints:

- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/logs`
- `POST /jobs/{job_id}/cancel`
- `DELETE /jobs/{job_id}`
- `POST /queue/pause`
- `POST /queue/cancel`
- `DELETE /queue`
- `GET /config`
- `POST /config`
- `GET /credentials`
- `POST /credentials`
- `GET /catalog/artists`
- `GET /catalog/years`
- `GET /catalog/releases`
- `GET /history`

Example video-only job:

```bash
curl -sS -X POST http://127.0.0.1:8090/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "urls": ["https://play.nugs.net/watch/release/38966"],
    "download_audio": false,
    "download_video": true,
    "video_format": 5,
    "video_output_format": "mkv"
  }'
```

Download history is stored in `download_history.sqlite3`.

## Docker

Build the image:

```bash
docker build -f docker/Dockerfile -t nugs-downloader:latest .
```

Run with Docker Compose:

```bash
mkdir -p data/private_sources/nugs/inbox data/private_sources/nugs/secrets
touch download_history.sqlite3
docker compose -f docker/docker-compose.yml up -d --build
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path ".\data\private_sources\nugs\inbox" | Out-Null
New-Item -ItemType Directory -Force -Path ".\data\private_sources\nugs\secrets" | Out-Null
New-Item -ItemType File -Force -Path .\download_history.sqlite3 | Out-Null
docker compose -f .\docker\docker-compose.yml up -d --build
```

Stop the service:

```bash
docker compose -f docker/docker-compose.yml down
```

Open:

```text
http://127.0.0.1:8090/
```

## Packaging

Build clean local packages:

```bash
./p3venv/bin/python packaging/build_packages.py
```

Outputs:

- `dist/NugsDownloader-macOS.dmg`
- `dist/NugsDownloader-macOS-app.zip`
- `dist/NugsDownloader-Windows-portable.zip`

The packaging script uses an allowlist and excludes credentials, token files, history DBs, tests, scripts, Docker files, README, git metadata, agent metadata, virtualenvs, and local data folders.

Packaged apps launch the local UI in a standalone desktop window through `pywebview`. They do not intentionally open the default browser; if a platform WebView backend is missing, the launcher exits with an error so the packaging/runtime issue is visible.

The macOS app icon and in-app header icon use original project artwork generated from `web/pot-leaf.svg`; no downloaded nugs.net logo or third-party icon pack is bundled.

To build a real Windows executable, use the GitHub Actions workflow or run the included PowerShell builder on Windows:

```powershell
Expand-Archive -Path .\dist\NugsDownloader-Windows-portable.zip -DestinationPath .\dist\windows-expanded -Force
Set-Location .\dist\windows-expanded\NugsDownloader
.\build-exe.ps1
```

The resulting executable is:

```text
dist/windows-expanded/NugsDownloader/dist/Nugs Downloader.exe
```

## GitHub Actions

The packaging workflow is defined in:

```text
.github/workflows/package.yml
```

It runs on pull requests and manual dispatch:

- Ubuntu job runs the unit test suite.
- macOS job builds and uploads the DMG and app zip.
- Windows job builds and uploads the real `.exe` plus the portable zip.
- Both packaging jobs check artifacts for forbidden private files.

## Tests

Run the test suite:

```bash
./p3venv/bin/python -m unittest discover -s tests -v
```

The tests cover URL parsing, mode enforcement, output formats, catalog filters, queue behavior, credential storage, web UI wiring, packaging allowlists, and GitHub Actions packaging expectations.

## Supported Media

| Type | URL example |
| --- | --- |
| Album | `https://play.nugs.net/release/23329` |
| Watch release | `https://play.nugs.net/watch/release/38966` |
| Artist | `https://play.nugs.net/#/artist/461/latest` |
| Catalog playlist | `https://2nu.gs/3PmqXLW` |
| Exclusive livestream | `https://play.nugs.net/watch/livestreams/exclusive/30119` |
| Purchased livestream | `https://www.nugs.net/on/demandware.store/Sites-NugsNet-Site/default/Stash-QueueVideo?...` |
| User playlist | `https://play.nugs.net/#/playlists/playlist/1215400` |
| Video | `https://play.nugs.net/#/videos/artist/1045/Dead%20and%20Company/container/27323` |
| Webcast | `https://play.nugs.net/#/my-webcasts/5826189-30369-0-624602` |

## Disclaimer

- You are responsible for how you use this project.
- Nugs brand and name are registered trademarks of their respective owner.
- This project has no partnership, sponsorship, or endorsement with Nugs.
