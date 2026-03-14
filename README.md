# Nugs-Downloader

Python downloader and FastAPI server for nugs.net content.

Original Go project: https://github.com/Sorrow446/Nugs-Downloader

## Requirements

- Python 3.10+
- ffmpeg available on `PATH` or present as `./ffmpeg`
- valid nugs.net credentials in `config.json`

Install dependencies:

```bash
python -m venv p3venv
./p3venv/bin/pip install -r requirements.txt
```

## Config

Copy `config.example.json` to `config.json` and set your local credentials there.
`config.json` is intentionally gitignored and must not be committed.

| Option | Info |
| --- | --- |
| email | Email address. |
| password | Password. |
| format | Track download quality. `1=ALAC`, `2=FLAC`, `3=MQA`, `4=360`, `5=AAC`. |
| videoFormat | Video quality. `1=480p`, `2=720p`, `3=1080p`, `4=1440p`, `5=4K`. |
| outPath | Output directory. Created automatically if missing. |
| token | Token for Apple/Google logins. See `token.md`. |
| useFfmpegEnvVar | `true` to use ffmpeg from `PATH`, `false` to use `./ffmpeg`. |

## Supported Media

| Type | URL example |
| --- | --- |
| Album | `https://play.nugs.net/release/23329` |
| Artist | `https://play.nugs.net/#/artist/461/latest` |
| Catalog playlist | `https://2nu.gs/3PmqXLW` |
| Exclusive livestream | `https://play.nugs.net/watch/livestreams/exclusive/30119` |
| Purchased livestream | `https://www.nugs.net/on/demandware.store/Sites-NugsNet-Site/default/Stash-QueueVideo?...` |
| User playlist | `https://play.nugs.net/#/playlists/playlist/1215400` |
| Video | `https://play.nugs.net/#/videos/artist/1045/Dead%20and%20Company/container/27323` |
| Webcast | `https://play.nugs.net/#/my-webcasts/5826189-30369-0-624602` |

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

Download from a URL and text files:

```bash
./p3venv/bin/python main.py https://play.nugs.net/release/23329 urls1.txt urls2.txt
```

Download a video:

```bash
./p3venv/bin/python main.py -F 1 --force-video https://play.nugs.net/watch/release/38966
```

## REST API Server

Start the API server:

```bash
./p3venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8090
```

Open the web interface at `http://127.0.0.1:8090/`.

Main endpoints:

- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/logs`
- `POST /jobs/{job_id}/cancel`
- `DELETE /jobs/{job_id}`
- `GET /config`
- `POST /config`
- `GET /history`
- `GET /history/lookup`
- `GET /history/successes`

Example job submission:

```bash
curl -sS -X POST http://127.0.0.1:8090/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "urls": ["https://play.nugs.net/watch/release/38966"],
    "download_audio": false,
    "download_video": true,
    "video_format": 1
  }'
```

`POST /config` controls the server's concurrency limit. Jobs above the limit stay queued until a slot is free. Download history is stored in `download_history.sqlite3`.

## Install As Linux Service

You can install the API as a systemd service with [scripts/install_linux_service.sh](scripts/install_linux_service.sh).

Install using an existing config file:

```bash
sudo ./scripts/install_linux_service.sh --config ./config.json
```

Install and prompt for email/password:

```bash
sudo ./scripts/install_linux_service.sh --prompt
```

Useful options:

- `--host 0.0.0.0`
- `--port 8090`
- `--service-name nugs-downloader`
- `--python /path/to/python`
- `--skip-enable` (install unit file only)

The installer writes service config to `/etc/<service-name>/config.json`, configures `NUGS_CONFIG_PATH` for the service process, and installs `/etc/systemd/system/<service-name>.service`.

Service management commands:

```bash
sudo systemctl status nugs-downloader.service
sudo systemctl restart nugs-downloader.service
sudo journalctl -u nugs-downloader.service -f
```

## Disclaimer

- You are responsible for how you use this project.
- Nugs brand and name are registered trademarks of their respective owner.
- This project has no partnership, sponsorship, or endorsement with Nugs.
