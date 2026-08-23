#!/usr/bin/env python3
"""REST API server for the Nugs downloader.

The server wraps `main.py`, queues jobs, runs them with configurable
parallelism, and persists download history in SQLite.

Usage:
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8090
"""

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
import main as nugs
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from subprocess import PIPE, Popen
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from nugs_credentials import credential_paths, read_credentials, write_credentials
from pydantic import BaseModel, Field

# --- Config ----------------------------------------------------------------

WORKDIR = Path(__file__).resolve().parent
WEB_INDEX_PATH = WORKDIR / "web" / "index.html"
WEB_FAVICON_PATH = WORKDIR / "web" / "pot-leaf.svg"

# Use the same Python interpreter that runs this server (typically the virtualenv)
import sys
PYTHON = sys.executable
DOWNLOADER_SCRIPT = WORKDIR / "main.py"
HISTORY_DB_PATH = Path(os.environ.get("NUGS_HISTORY_DB_PATH", WORKDIR / "download_history.sqlite3")).expanduser()
FFMPEG_BIN = str((WORKDIR / "ffmpeg").resolve()) if (WORKDIR / "ffmpeg").exists() else "ffmpeg"


def _default_out_path() -> str:
    override = os.environ.get("NUGS_DEFAULT_OUT_PATH", "").strip()
    if override:
        return str(Path(override).expanduser())
    return str(Path.home() / "Music" / "Nugs Downloader")


class ConfigResponse(BaseModel):
    max_concurrent_jobs: int
    pending_jobs: int
    running_jobs: int
    queue_paused: bool
    default_out_path: str


class ConfigUpdate(BaseModel):
    max_concurrent_jobs: int = Field(..., gt=0)


class CredentialsUpdate(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None

# Keep logs bounded per job.
LOG_MAX_LINES = 1000
LOG_FLUSH_SECONDS = 10


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DownloadRequest(BaseModel):
    urls: List[str] = Field(..., description="List of release/playlist/video URLs or .txt paths")
    format: Optional[int] = Field(None, ge=1, le=5, description="Audio format (1-5)")
    video_format: Optional[int] = Field(None, ge=1, le=5, description="Video format (1-5)")
    audio_output_format: Optional[str] = Field(None, description="Final audio output format")
    video_output_format: Optional[str] = Field(None, description="Final video output container")
    track_ids: Optional[List[int]] = Field(None, description="Track IDs to download from a release")
    out_path: Optional[str] = Field(None, description="Output folder")
    download_audio: Optional[bool] = Field(True, description="Download audio tracks")
    download_video: Optional[bool] = Field(True, description="Download video tracks")
    download_if_already_downloaded: Optional[bool] = Field(
        False,
        description="If false, URLs that already completed successfully will be skipped",
    )
    skip_chapters: Optional[bool] = Field(False, description="Skip embedding chapters")


class BulkDownloadRequest(BaseModel):
    scope: str = Field(..., description="band, year, or all")
    artist_id: Optional[int] = Field(None, description="Artist ID for band scope")
    year: Optional[str] = Field(None, description="Performance year for year scope")
    format: Optional[int] = Field(None, ge=1, le=5, description="Audio format (1-5)")
    video_format: Optional[int] = Field(None, ge=1, le=5, description="Video format (1-5)")
    audio_output_format: Optional[str] = Field(None, description="Final audio output format")
    video_output_format: Optional[str] = Field(None, description="Final video output container")
    track_ids: Optional[List[int]] = Field(None, description="Track IDs to download from each release")
    out_path: Optional[str] = Field(None, description="Output folder")
    download_audio: Optional[bool] = Field(True, description="Download audio tracks")
    download_video: Optional[bool] = Field(True, description="Download video tracks")
    download_if_already_downloaded: Optional[bool] = Field(
        False,
        description="If false, URLs that already completed successfully will be skipped",
    )
    skip_chapters: Optional[bool] = Field(False, description="Skip embedding chapters")


@dataclass
class Job:
    id: str
    request: DownloadRequest
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    proc: Optional[Popen] = None
    progress: Optional[Dict[str, object]] = None
    file_events: List[Dict[str, object]] = field(default_factory=list)
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_MAX_LINES))

    def append_log(self, line: str) -> None:
        self.logs.append(f"[{datetime.utcnow().isoformat()}] {line}")


app = FastAPI(title="Nugs Downloader API")

jobs: Dict[str, Job] = {}
jobs_lock = threading.Lock()
history_lock = threading.Lock()
bulk_tasks_lock = threading.Lock()
bulk_cancel_event = threading.Event()

# Concurrency / queue support
max_concurrent_jobs = 2
pending_queue: deque[str] = deque()
queue_paused = True
bulk_tasks: Dict[str, Dict[str, object]] = {}
catalog_artists_cache: Optional[List[Dict[str, object]]] = None
catalog_releases_cache: Dict[int, List[Dict[str, object]]] = {}
catalog_artist_media_cache: Dict[str, set[int]] = {}
catalog_release_detail_cache: Dict[int, Dict[str, object]] = {}


def _normalize_url(url: str) -> str:
    # strip the "/watch" prefix if present
    return url.replace("/watch/", "/")


def _config_path() -> Path:
    return nugs.get_config_path()


def _read_config_json() -> Dict[str, object]:
    config_path = _config_path()
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _write_config_json(data: Dict[str, object]) -> None:
    config_path = _config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _has_session_credentials() -> bool:
    encrypted = read_credentials(_config_path())
    return bool(encrypted.get("email") and encrypted.get("password"))


def _clear_catalog_caches() -> None:
    global catalog_artists_cache
    catalog_artists_cache = None
    catalog_releases_cache.clear()
    catalog_artist_media_cache.clear()
    catalog_release_detail_cache.clear()


def _release_url(container_id: object) -> str:
    return f"https://play.nugs.net/release/{container_id}"


def _product_formats(container: Dict[str, object]) -> List[str]:
    products: List[object] = []
    for key in ("productFormatList", "products"):
        value = container.get(key) or []
        if isinstance(value, list):
            products.extend(value)
    return [
        str(product.get("formatStr") or product.get("pfTypeStr") or "")
        for product in products
        if isinstance(product, dict)
    ]


def _release_summary(container: Dict[str, object]) -> Dict[str, object]:
    container_id = container.get("containerID") or container.get("ContainerID")
    formats = _product_formats(container)
    video_chapters = container.get("videoChapters")
    has_video = (
        any(fmt in ("VIDEO ON DEMAND", "LIVE HD VIDEO") for fmt in formats)
        or bool(container.get("svodskuID"))
        or bool(container.get("vodPlayerImage"))
        or (isinstance(video_chapters, list) and len(video_chapters) > 0)
    )
    has_audio = bool(container.get("songs")) or any(
        fmt in ("MP3", "FLAC", "FLAC-HD", "ALAC", "ALAC-HD", "MQA") for fmt in formats
    )
    return {
        "id": container_id,
        "url": _release_url(container_id),
        "artist_id": container.get("artistID") or container.get("ArtistID"),
        "artist_name": container.get("artistName") or container.get("ArtistName") or "",
        "title": container.get("containerInfo") or container.get("ContainerInfo") or "",
        "venue": container.get("venue") or "",
        "date": container.get("performanceDateFormatted") or container.get("performanceDate") or "",
        "year": str(container.get("performanceDateYear") or ""),
        "type": container.get("containerTypeStr") or "",
        "available": container.get("availabilityTypeStr") or "",
        "has_audio": has_audio,
        "has_video": has_video,
    }


def _download_mode_label(req: DownloadRequest) -> str:
    if req.download_audio and req.download_video:
        return "Audio + video"
    if req.download_audio:
        return "Audio only"
    if req.download_video:
        return "Video only"
    return "No media selected"


def _audio_quality_label(value: Optional[int]) -> str:
    labels = {
        1: "ALAC 16/44.1",
        2: "FLAC 16/44.1",
        3: "MQA 24/48",
        4: "360 Reality",
        5: "AAC 150",
    }
    return labels.get(value or 0, "Config default")


def _video_quality_label(value: Optional[int]) -> str:
    labels = {
        1: "480p",
        2: "720p",
        3: "1080p",
        4: "1440p",
        5: "4K / best",
    }
    return labels.get(value or 0, "Config default")


def _request_release_id(req: DownloadRequest) -> Optional[int]:
    for url in req.urls:
        match = re.search(r"play\.nugs\.net/(?:watch/)?release/(\d+)", _normalize_url(url))
        if match:
            return int(match.group(1))
    return None


def _cached_release_detail(release_id: int) -> Dict[str, object]:
    if release_id in catalog_release_detail_cache:
        return catalog_release_detail_cache[release_id]

    meta = nugs.get_album_meta(str(release_id)).get("Response", {})
    detail = {
        "release_id": release_id,
        "artist_name": meta.get("artistName", ""),
        "title": meta.get("containerInfo", ""),
        "venue": meta.get("venue", ""),
        "date": meta.get("performanceDateFormatted") or meta.get("performanceDate") or "",
        "year": str(meta.get("performanceDateYear") or ""),
        "has_audio": bool(meta.get("songs")),
        "has_video": any(fmt in ("VIDEO ON DEMAND", "LIVE HD VIDEO") for fmt in _product_formats(meta)),
    }
    catalog_release_detail_cache[release_id] = detail
    return detail


def _request_summary(req: DownloadRequest) -> Dict[str, object]:
    release_id = _request_release_id(req)
    out_path = req.out_path or _default_out_path()
    summary: Dict[str, object] = {
        "urls": [_normalize_url(url) for url in req.urls],
        "mode": _download_mode_label(req),
        "track_count": len(req.track_ids or []),
        "out_path": out_path,
    }
    if req.download_audio:
        summary["audio_quality"] = _audio_quality_label(req.format)
        summary["audio_output_format"] = req.audio_output_format or "source"
    if req.download_video:
        summary["video_quality"] = _video_quality_label(req.video_format)
        summary["video_output_format"] = req.video_output_format or "mkv"
    if release_id is not None:
        summary["release_id"] = release_id
        try:
            summary.update(_cached_release_detail(release_id))
        except Exception as exc:
            summary["metadata_error"] = str(exc)
    return summary


def _fetch_artists() -> List[Dict[str, object]]:
    global catalog_artists_cache
    if catalog_artists_cache is not None:
        return catalog_artists_cache

    r = nugs.session.get(
        nugs.streamApiBase + "api.aspx",
        params={"method": "catalog.artists", "limit": "5000"},
        headers={"User-Agent": nugs.userAgent},
    )
    if r.status_code != 200:
        raise RuntimeError(f"Artist catalog failed: {r.status_code} {r.reason}")
    artists = r.json().get("Response", {}).get("artists", [])
    catalog_artists_cache = []
    for artist in artists:
        if not isinstance(artist, dict) or not artist.get("artistID"):
            continue
        name = str(artist.get("artistName", "")).strip()
        if not name:
            continue
        num_shows = int(artist.get("numShows") or 0)
        num_albums = int(artist.get("numAlbums") or 0)
        if num_shows + num_albums < 1:
            continue
        catalog_artists_cache.append(
            {
                "id": artist.get("artistID"),
                "name": name,
                "num_shows": num_shows,
                "num_albums": num_albums,
            }
        )
    catalog_artists_cache.sort(key=lambda item: str(item["name"]).lower())
    return catalog_artists_cache


def _fetch_artist_releases(artist_id: int) -> List[Dict[str, object]]:
    if artist_id in catalog_releases_cache:
        return catalog_releases_cache[artist_id]

    releases: List[Dict[str, object]] = []
    offset = 1
    limit = 100
    while True:
        r = nugs.session.get(
            nugs.streamApiBase + "api.aspx",
            params={
                "method": "catalog.containersAll",
                "artistList": str(artist_id),
                "limit": str(limit),
                "startOffset": str(offset),
                "availType": "1",
                "vdisp": "1",
            },
            headers={"User-Agent": nugs.userAgent},
        )
        if r.status_code != 200:
            raise RuntimeError(f"Artist releases failed: {r.status_code} {r.reason}")
        response = r.json().get("Response", {})
        containers = response.get("containers") or response.get("Containers") or []
        if not containers:
            break
        releases.extend(_release_summary(c) for c in containers if isinstance(c, dict))
        if len(containers) < limit:
            break
        offset += len(containers)

    releases.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    catalog_releases_cache[artist_id] = releases
    return releases


def _fetch_artist_release_page(artist_id: int, offset: int = 1, limit: int = 100) -> List[Dict[str, object]]:
    r = nugs.session.get(
        nugs.streamApiBase + "api.aspx",
        params={
            "method": "catalog.containersAll",
            "artistList": str(artist_id),
            "limit": str(limit),
            "startOffset": str(offset),
            "availType": "1",
            "vdisp": "1",
        },
        headers={"User-Agent": nugs.userAgent},
        timeout=12,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Artist releases failed: {r.status_code} {r.reason}")
    response = r.json().get("Response", {})
    containers = response.get("containers") or response.get("Containers") or []
    return [_release_summary(c) for c in containers if isinstance(c, dict)]


def _matches_media_filter(release: Dict[str, object], media: Optional[str]) -> bool:
    if media == "audio":
        return bool(release.get("has_audio"))
    if media == "audio_only":
        return bool(release.get("has_audio")) and not bool(release.get("has_video"))
    if media == "video":
        return bool(release.get("has_video"))
    return True


def _artist_ids_for_media(media: Optional[str], artists: List[Dict[str, object]]) -> Optional[set[int]]:
    if media not in {"audio", "audio_only", "video"}:
        return None
    if media == "video":
        matching_ids = {
            int(artist.get("id") or 0)
            for artist in artists
            if artist.get("id") and int(artist.get("num_shows") or 0) > 0
        }
        catalog_artist_media_cache[media] = matching_ids
        return matching_ids
    if media == "audio":
        matching_ids = {
            int(artist.get("id") or 0)
            for artist in artists
            if artist.get("id") and int(artist.get("num_albums") or 0) > 0
        }
        catalog_artist_media_cache[media] = matching_ids
        return matching_ids
    if media in catalog_artist_media_cache:
        cached_ids = catalog_artist_media_cache[media]
        return {
            int(artist.get("id") or 0)
            for artist in artists
            if int(artist.get("id") or 0) in cached_ids
        }

    matching_ids: set[int] = set()
    artist_ids = [
        int(artist.get("id") or 0)
        for artist in artists
        if artist.get("id") and (media != "video" or int(artist.get("num_shows") or 0) > 0)
    ]

    def artist_has_media(artist_id: int) -> Optional[int]:
        try:
            releases = catalog_releases_cache.get(artist_id) or _fetch_artist_release_page(artist_id)
        except Exception:
            return None
        if any(_matches_media_filter(release, media) for release in releases):
            return artist_id
        return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(artist_has_media, artist_id) for artist_id in artist_ids]
        for future in as_completed(futures):
            artist_id = future.result()
            if artist_id is not None:
                matching_ids.add(artist_id)

    catalog_artist_media_cache[media] = matching_ids
    return matching_ids


def _catalog_years_for_media(media: Optional[str], artists: List[Dict[str, object]]) -> List[str]:
    matching_ids = _artist_ids_for_media(media, artists)
    candidate_artists = artists
    if matching_ids is not None:
        candidate_artists = [artist for artist in artists if int(artist.get("id") or 0) in matching_ids]

    years: set[str] = set()
    for artist in candidate_artists:
        artist_id = int(artist.get("id") or 0)
        if not artist_id:
            continue
        try:
            releases = catalog_releases_cache.get(artist_id) or _fetch_artist_release_page(artist_id)
        except Exception:
            continue
        years.update(
            str(release.get("year"))
            for release in releases
            if release.get("year") and _matches_media_filter(release, media)
        )
    return sorted(years, reverse=True)


def _enqueue_request(req: DownloadRequest) -> Dict[str, object]:
    job_id = str(uuid.uuid4())
    job = Job(id=job_id, request=req)
    should_start = False

    with jobs_lock:
        jobs[job_id] = job
        if not queue_paused and _running_job_count_unlocked() < max_concurrent_jobs:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.utcnow()
            should_start = True
        else:
            job.status = JobStatus.PENDING
            pending_queue.append(job_id)

    _history_upsert(job)

    if should_start:
        _start_job(job)

    return {
        "job_id": job_id,
        "status": job.status,
        "queued_urls": [_normalize_url(u) for u in req.urls],
    }


def _init_history_db() -> None:
    with history_lock:
        with sqlite3.connect(HISTORY_DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS download_history (
                    job_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    urls_json TEXT NOT NULL,
                    out_path TEXT,
                    download_audio INTEGER NOT NULL,
                    download_video INTEGER NOT NULL,
                    error TEXT,
                    files_json TEXT
                )
                """
            )
            conn.commit()


def _history_upsert(job: Job) -> None:
    payload = (
        job.id,
        job.created_at.isoformat(),
        job.finished_at.isoformat() if job.finished_at else None,
        job.status.value,
        json.dumps([_normalize_url(u) for u in job.request.urls]),
        job.request.out_path,
        1 if job.request.download_audio else 0,
        1 if job.request.download_video else 0,
        job.error,
        json.dumps(job.file_events),
    )
    with history_lock:
        with sqlite3.connect(HISTORY_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO download_history (
                    job_id, created_at, finished_at, status, urls_json, out_path,
                    download_audio, download_video, error, files_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    finished_at=excluded.finished_at,
                    status=excluded.status,
                    urls_json=excluded.urls_json,
                    out_path=excluded.out_path,
                    download_audio=excluded.download_audio,
                    download_video=excluded.download_video,
                    error=excluded.error,
                    files_json=excluded.files_json
                """,
                payload,
            )
            conn.commit()


_init_history_db()


def _get_successfully_downloaded_urls() -> set[str]:
    with history_lock:
        with sqlite3.connect(HISTORY_DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT urls_json
                FROM download_history
                WHERE status = ?
                """,
                (JobStatus.SUCCESS.value,),
            ).fetchall()

    success_urls: set[str] = set()
    for row in rows:
        try:
            row_urls = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            continue
        for url in row_urls:
            if isinstance(url, str):
                success_urls.add(_normalize_url(url))
    return success_urls


def _make_cmd(req: DownloadRequest) -> List[str]:
    if getattr(sys, "frozen", False):
        cmd = [PYTHON, "--nugs-worker"]
    else:
        cmd = [PYTHON, "-u", str(DOWNLOADER_SCRIPT)]
    if req.audio_output_format and req.audio_output_format not in ("source", "mp3", "flac", "m4a"):
        raise ValueError("audio_output_format must be source, mp3, flac, or m4a")
    if req.video_output_format and req.video_output_format not in ("mkv", "mp4", "mov"):
        raise ValueError("video_output_format must be mkv, mp4, or mov")
    if req.format is not None:
        cmd += ["-f", str(req.format)]
    if req.video_format is not None:
        cmd += ["-F", str(req.video_format)]
    if req.audio_output_format:
        cmd += ["--audio-output-format", req.audio_output_format]
    if req.video_output_format:
        cmd += ["--video-output-format", req.video_output_format]
    if req.track_ids:
        cmd += ["--track-ids", ",".join(str(track_id) for track_id in req.track_ids)]
    cmd += ["-o", req.out_path or _default_out_path()]

    # audio/video selection
    if not req.download_audio and req.download_video:
        cmd.append("--force-video")
    if req.download_audio and not req.download_video:
        cmd.append("--skip-videos")
    if not req.download_audio and not req.download_video:
        raise ValueError("At least one of download_audio or download_video must be true")

    if req.skip_chapters:
        cmd.append("--skip-chapters")

    cmd += [_normalize_url(u) for u in req.urls]
    return cmd


def _running_job_count_unlocked() -> int:
    return sum(1 for job in jobs.values() if job.status == JobStatus.RUNNING)


def _start_job(job: Job) -> None:
    thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    thread.start()


def _dispatch_next_jobs() -> None:
    """Start queued jobs until the configured concurrency limit is reached."""
    jobs_to_start: List[Job] = []

    with jobs_lock:
        if queue_paused:
            return
        available_slots = max(0, max_concurrent_jobs - _running_job_count_unlocked())
        while available_slots > 0 and pending_queue:
            next_job_id = pending_queue.popleft()
            next_job = jobs.get(next_job_id)
            if not next_job or next_job.status != JobStatus.PENDING:
                continue
            next_job.status = JobStatus.RUNNING
            next_job.started_at = datetime.utcnow()
            jobs_to_start.append(next_job)
            available_slots -= 1

    for job in jobs_to_start:
        _start_job(job)


def _record_file_event(job: Job, event: Dict[str, object]) -> None:
    path = str(event.get("path", ""))
    state = str(event.get("state", ""))
    kind = str(event.get("kind", ""))
    if not path:
        return

    for i, existing in enumerate(job.file_events):
        if str(existing.get("path", "")) == path:
            if state == "created" or str(existing.get("state", "")) != "created":
                job.file_events[i] = event
            return
    job.file_events.append(event)


def _try_parse_json_marker(line: str, marker: str) -> Optional[Dict[str, object]]:
    prefix = marker + " "
    if not line.startswith(prefix):
        return None
    payload_str = line[len(prefix):].strip()
    if not payload_str:
        return None
    try:
        parsed = json.loads(payload_str)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _run_job(job: Job) -> None:
    if job.started_at is None:
        job.started_at = datetime.utcnow()
    job.status = JobStatus.RUNNING
    job.progress = {
        "kind": "job",
        "message": "started",
        "updated_at": datetime.utcnow().isoformat(),
    }

    cmd = _make_cmd(job.request)
    proc = Popen(cmd, cwd=str(WORKDIR), stdout=PIPE, stderr=PIPE, text=True)
    job.proc = proc

    last_flush = datetime.utcnow()

    def _read_stream(stream, prefix: str) -> None:
        nonlocal last_flush
        for line in stream:
            line = line.rstrip("\n")

            progress = _try_parse_json_marker(line, "PROGRESS")
            if progress is not None:
                progress["updated_at"] = datetime.utcnow().isoformat()
                with jobs_lock:
                    job.progress = progress

            file_event = _try_parse_json_marker(line, "FILE")
            if file_event is not None:
                with jobs_lock:
                    _record_file_event(job, file_event)

            job.append_log(f"{prefix}{line}")
            # Flush logs every LOG_FLUSH_SECONDS to avoid huge memory growth.
            if (datetime.utcnow() - last_flush).total_seconds() >= LOG_FLUSH_SECONDS:
                last_flush = datetime.utcnow()

    stdout_thread = threading.Thread(target=_read_stream, args=(proc.stdout, "OUT: "), daemon=True)
    stderr_thread = threading.Thread(target=_read_stream, args=(proc.stderr, "ERR: "), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        proc.wait()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)

        job.exit_code = proc.returncode
        job.finished_at = datetime.utcnow()
        # If the job was cancelled externally, preserve cancelled status.
        if job.status != JobStatus.CANCELLED:
            if proc.returncode == 0:
                job.status = JobStatus.SUCCESS
            else:
                job.status = JobStatus.FAILED
                recent_logs = [line for line in list(job.logs)[-12:] if line.strip()]
                job.error = f"Exit code {proc.returncode}"
                if recent_logs:
                    job.error += ": " + " | ".join(recent_logs[-4:])
        _history_upsert(job)
    finally:
        _dispatch_next_jobs()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def web_ui() -> str:
    if not WEB_INDEX_PATH.exists():
        raise HTTPException(status_code=404, detail="Web UI file not found")
    return WEB_INDEX_PATH.read_text(encoding="utf-8")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    if not WEB_FAVICON_PATH.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(str(WEB_FAVICON_PATH), media_type="image/svg+xml")


@app.post("/jobs", status_code=201)
def create_job(req: DownloadRequest):
    try:
        _make_cmd(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    skipped_urls: List[str] = []
    if not req.download_if_already_downloaded:
        successful_urls = _get_successfully_downloaded_urls()
        filtered_urls: List[str] = []
        for raw_url in req.urls:
            normalized_url = _normalize_url(raw_url)
            if normalized_url in successful_urls:
                skipped_urls.append(normalized_url)
            else:
                filtered_urls.append(raw_url)

        if not filtered_urls:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "All requested URLs were already downloaded successfully",
                    "already_downloaded_urls": skipped_urls,
                },
            )

        req_data = req.model_dump() if hasattr(req, "model_dump") else req.dict()  # type: ignore[attr-defined]
        req_data["urls"] = filtered_urls
        req = DownloadRequest(**req_data)

    created = _enqueue_request(req)

    return {
        **created,
        "skipped_urls": skipped_urls,
    }


@app.get("/catalog/artists")
def catalog_artists(q: Optional[str] = None, media: Optional[str] = None):
    if not _has_session_credentials():
        return {"count": 0, "items": []}
    try:
        artists = _fetch_artists()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if q:
        needle = q.lower()
        artists = [artist for artist in artists if needle in str(artist.get("name", "")).lower()]
    try:
        matching_artist_ids = _artist_ids_for_media(media, artists)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if matching_artist_ids is not None:
        artists = [artist for artist in artists if int(artist.get("id") or 0) in matching_artist_ids]
    return {"count": len(artists), "items": artists}


@app.get("/catalog/years")
def catalog_years(media: Optional[str] = None):
    if not _has_session_credentials():
        return {"count": 0, "items": []}
    try:
        artists = _fetch_artists()
        years = _catalog_years_for_media(media, artists)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"count": len(years), "items": years}


@app.get("/catalog/artists/{artist_id}/releases")
def catalog_artist_releases(artist_id: int, year: Optional[str] = None):
    if not _has_session_credentials():
        return {"count": 0, "years": [], "items": []}
    try:
        releases = _fetch_artist_releases(artist_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if year:
        releases = [release for release in releases if release.get("year") == year]
    years = sorted({str(release.get("year")) for release in releases if release.get("year")}, reverse=True)
    return {"count": len(releases), "years": years, "items": releases}


@app.get("/catalog/releases/{release_id}")
def catalog_release_detail(release_id: int):
    if not _has_session_credentials():
        raise HTTPException(status_code=401, detail="Login required")
    try:
        meta = nugs.get_album_meta(str(release_id)).get("Response", {})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    songs = []
    for idx, song in enumerate(meta.get("songs", []), start=1):
        if not isinstance(song, dict):
            continue
        songs.append(
            {
                "track_id": song.get("trackID"),
                "title": song.get("songTitle", ""),
                "disc": song.get("discNum"),
                "track": song.get("trackNum") or idx,
                "set": song.get("setNum"),
            }
        )

    return {
        "id": release_id,
        "artist_name": meta.get("artistName", ""),
        "title": meta.get("containerInfo", ""),
        "songs": songs,
    }


def _update_bulk_task(bulk_id: str, **updates: object) -> None:
    with bulk_tasks_lock:
        task = bulk_tasks.get(bulk_id)
        if task is None:
            return
        task.update(updates)
        task["updated_at"] = datetime.utcnow().isoformat()


def _bulk_cancel_requested(bulk_id: str) -> bool:
    if bulk_cancel_event.is_set():
        return True
    with bulk_tasks_lock:
        return bool(bulk_tasks.get(bulk_id, {}).get("cancel_requested"))


def _mark_bulk_cancelled(
    bulk_id: str,
    created_count: int = 0,
    skipped_count: int = 0,
    failed_count: int = 0,
) -> None:
    _update_bulk_task(
        bulk_id,
        status="cancelled",
        cancel_requested=True,
        created_count=created_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        current_url=None,
        finished_at=datetime.utcnow().isoformat(),
    )


def _iter_bulk_releases(req: BulkDownloadRequest):
    if req.scope == "band":
        if req.artist_id is None:
            raise ValueError("artist_id is required for band scope")
        releases = _fetch_artist_releases(req.artist_id)
        for release in sorted(releases, key=lambda item: str(item.get("date", ""))):
            yield release
        return

    if req.scope == "year":
        if not req.year:
            raise ValueError("year is required for year scope")
        for artist in _fetch_artists():
            artist_id = artist.get("id")
            if artist_id is None:
                continue
            releases = [
                release
                for release in _fetch_artist_releases(int(artist_id))
                if release.get("year") == req.year
            ]
            for release in sorted(releases, key=lambda item: str(item.get("date", ""))):
                yield release
        return

    if req.scope == "all":
        for artist in _fetch_artists():
            artist_id = artist.get("id")
            if artist_id is None:
                continue
            releases = _fetch_artist_releases(int(artist_id))
            for release in sorted(releases, key=lambda item: str(item.get("date", ""))):
                yield release
        return

    raise ValueError("scope must be band, year, or all")


def _run_bulk_queue(bulk_id: str, req: BulkDownloadRequest) -> None:
    created_count = 0
    skipped_count = 0
    failed_count = 0
    skipped_urls: List[str] = []
    failed_urls: List[str] = []
    successful_urls = _get_successfully_downloaded_urls() if not req.download_if_already_downloaded else set()

    if _bulk_cancel_requested(bulk_id):
        _mark_bulk_cancelled(bulk_id, created_count, skipped_count, failed_count)
        return

    _update_bulk_task(bulk_id, status="running")
    try:
        for release in _iter_bulk_releases(req):
            if _bulk_cancel_requested(bulk_id):
                _mark_bulk_cancelled(bulk_id, created_count, skipped_count, failed_count)
                return

            url = str(release.get("url") or "")
            if not url:
                continue

            normalized_url = _normalize_url(url)
            if normalized_url in successful_urls:
                skipped_count += 1
                skipped_urls.append(normalized_url)
                _update_bulk_task(
                    bulk_id,
                    skipped_count=skipped_count,
                    skipped_urls=skipped_urls[-50:],
                    current_url=normalized_url,
                )
                continue

            download_req = DownloadRequest(
                urls=[url],
                format=req.format,
                video_format=req.video_format,
                audio_output_format=req.audio_output_format,
                video_output_format=req.video_output_format,
                track_ids=req.track_ids,
                out_path=req.out_path,
                download_audio=req.download_audio,
                download_video=req.download_video,
                download_if_already_downloaded=req.download_if_already_downloaded,
                skip_chapters=req.skip_chapters,
            )
            try:
                _make_cmd(download_req)
                _enqueue_request(download_req)
                created_count += 1
            except Exception:
                failed_count += 1
                failed_urls.append(normalized_url)

            if (created_count + skipped_count + failed_count) % 10 == 0:
                _update_bulk_task(
                    bulk_id,
                    created_count=created_count,
                    skipped_count=skipped_count,
                    failed_count=failed_count,
                    failed_urls=failed_urls[-50:],
                    current_url=normalized_url,
                )

        _update_bulk_task(
            bulk_id,
            status="complete",
            created_count=created_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            skipped_urls=skipped_urls[-50:],
            failed_urls=failed_urls[-50:],
            current_url=None,
            finished_at=datetime.utcnow().isoformat(),
        )
    except Exception as exc:
        _update_bulk_task(
            bulk_id,
            status="failed",
            created_count=created_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            error=str(exc),
            finished_at=datetime.utcnow().isoformat(),
        )


@app.post("/jobs/bulk", status_code=202)
def create_bulk_jobs(req: BulkDownloadRequest):
    if not req.download_audio and not req.download_video:
        raise HTTPException(status_code=400, detail="At least one of download_audio or download_video must be true")

    if req.scope == "band" and req.artist_id is None:
        raise HTTPException(status_code=400, detail="artist_id is required for band scope")
    if req.scope == "year" and not req.year:
        raise HTTPException(status_code=400, detail="year is required for year scope")
    if req.scope not in {"band", "year", "all"}:
        raise HTTPException(status_code=400, detail="scope must be band, year, or all")

    probe_req = DownloadRequest(
        urls=["https://play.nugs.net/release/0"],
        format=req.format,
        video_format=req.video_format,
        audio_output_format=req.audio_output_format,
        video_output_format=req.video_output_format,
        track_ids=req.track_ids,
        out_path=req.out_path,
        download_audio=req.download_audio,
        download_video=req.download_video,
        download_if_already_downloaded=req.download_if_already_downloaded,
        skip_chapters=req.skip_chapters,
    )
    try:
        _make_cmd(probe_req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    bulk_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    bulk_cancel_event.clear()
    with bulk_tasks_lock:
        bulk_tasks[bulk_id] = {
            "id": bulk_id,
            "status": "queued",
            "cancel_requested": False,
            "scope": req.scope,
            "artist_id": req.artist_id,
            "year": req.year,
            "created_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "created_at": now,
            "updated_at": now,
        }

    threading.Thread(target=_run_bulk_queue, args=(bulk_id, req), daemon=True).start()
    return {
        "bulk_id": bulk_id,
        "status": "queued",
        "created_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
    }


@app.get("/jobs/bulk/{bulk_id}")
def get_bulk_job(bulk_id: str):
    with bulk_tasks_lock:
        task = bulk_tasks.get(bulk_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Bulk queue task not found")
        return dict(task)


def _count_running_jobs() -> int:
    with jobs_lock:
        return _running_job_count_unlocked()


@app.get("/jobs")
def list_jobs():
    with jobs_lock:
        return [
            {
                "id": j.id,
                "status": j.status,
                "created_at": j.created_at.isoformat(),
                "queue_position": (pending_queue.index(j.id) + 1) if j.status == JobStatus.PENDING and j.id in pending_queue else None,
                "summary": _request_summary(j.request),
            }
            for j in jobs.values()
        ]


@app.get("/config")
def get_config():
    return ConfigResponse(
        max_concurrent_jobs=max_concurrent_jobs,
        pending_jobs=len(pending_queue),
        running_jobs=_count_running_jobs(),
        queue_paused=queue_paused,
        default_out_path=_default_out_path(),
    )


@app.post("/config")
def update_config(cfg: ConfigUpdate):
    global max_concurrent_jobs

    if cfg.max_concurrent_jobs < 1:
        raise HTTPException(status_code=400, detail="max_concurrent_jobs must be > 0")

    with jobs_lock:
        max_concurrent_jobs = cfg.max_concurrent_jobs
    _dispatch_next_jobs()

    return get_config()


@app.get("/credentials")
def get_credentials_status():
    cfg = _read_config_json()
    encrypted = read_credentials(_config_path())
    email = str(encrypted.get("email") or cfg.get("email") or "")
    password = encrypted.get("password") or cfg.get("password")
    token = encrypted.get("token") or cfg.get("token")
    has_saved_credentials = bool(encrypted.get("email") or encrypted.get("password") or encrypted.get("token"))
    paths = credential_paths(_config_path())
    return {
        "email": email if "@" in email and not email.startswith("your-") else "",
        "has_password": bool(password) and password != "your-password",
        "has_token": bool(token),
        "has_saved_credentials": has_saved_credentials,
        "config_path": str(_config_path()),
        "credentials_path": str(paths["credentials"]),
    }


@app.post("/credentials")
def update_credentials(creds: CredentialsUpdate):
    cfg = _read_config_json()
    existing = read_credentials(_config_path())
    email = (creds.email or "").strip() or str(existing.get("email") or cfg.get("email") or "").strip()
    password = creds.password if creds.password is not None else None
    token = creds.token if creds.token is not None else None

    next_credentials = {
        "email": email,
        "password": password or existing.get("password") or cfg.get("password") or "",
        "token": token.strip() if token is not None else existing.get("token") or cfg.get("token") or "",
    }
    write_credentials(_config_path(), next_credentials)

    for key in ("email", "password", "token"):
        cfg.pop(key, None)

    _write_config_json(cfg)
    return get_credentials_status()


@app.delete("/credentials")
def delete_credentials():
    cfg = _read_config_json()
    paths = credential_paths(_config_path())
    for key in ("email", "password", "token"):
        cfg.pop(key, None)
    for path in paths.values():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    _write_config_json(cfg)
    _clear_catalog_caches()
    return get_credentials_status()


@app.post("/queue/pause")
def pause_queue():
    global queue_paused
    with jobs_lock:
        queue_paused = True
    return get_config()


@app.post("/queue/resume")
def resume_queue():
    global queue_paused
    with jobs_lock:
        queue_paused = False
    _dispatch_next_jobs()
    return get_config()


def _cancel_running_job(job: Job) -> None:
    if job.proc and job.proc.poll() is None:
        try:
            job.proc.terminate()
            job.proc.wait(timeout=5)
        except Exception:
            try:
                job.proc.kill()
            except Exception:
                pass
    job.status = JobStatus.CANCELLED
    job.finished_at = datetime.utcnow()
    _history_upsert(job)


def _request_bulk_cancellation() -> int:
    cancelled_bulk_count = 0
    bulk_cancel_event.set()
    with bulk_tasks_lock:
        for task in bulk_tasks.values():
            if task.get("status") in {"queued", "running"}:
                task["cancel_requested"] = True
                task["status"] = "cancelled"
                task["finished_at"] = datetime.utcnow().isoformat()
                task["updated_at"] = datetime.utcnow().isoformat()
                cancelled_bulk_count += 1
    return cancelled_bulk_count


@app.post("/queue/cancel")
def cancel_queue():
    running_jobs: List[Job] = []
    cancelled_count = 0
    cancelled_bulk_count = _request_bulk_cancellation()

    with jobs_lock:
        for job in jobs.values():
            if job.status == JobStatus.PENDING:
                try:
                    pending_queue.remove(job.id)
                except ValueError:
                    pass
                job.status = JobStatus.CANCELLED
                job.finished_at = datetime.utcnow()
                _history_upsert(job)
                cancelled_count += 1
            elif job.status == JobStatus.RUNNING:
                running_jobs.append(job)

    for job in running_jobs:
        _cancel_running_job(job)
        cancelled_count += 1

    with jobs_lock:
        for job in jobs.values():
            if job.status == JobStatus.PENDING:
                try:
                    pending_queue.remove(job.id)
                except ValueError:
                    pass
                job.status = JobStatus.CANCELLED
                job.finished_at = datetime.utcnow()
                _history_upsert(job)
                cancelled_count += 1

    return {
        "status": "cancelled",
        "cancelled_count": cancelled_count,
        "cancelled_bulk_count": cancelled_bulk_count,
    }


@app.post("/queue/delete")
def delete_queue():
    deleted_count = 0
    skipped_running_count = 0
    cancelled_bulk_count = _request_bulk_cancellation()

    with jobs_lock:
        for job_id, job in list(jobs.items()):
            if job.status == JobStatus.RUNNING:
                skipped_running_count += 1
                continue
            if job.status == JobStatus.PENDING:
                try:
                    pending_queue.remove(job_id)
                except ValueError:
                    pass
            del jobs[job_id]
            deleted_count += 1

    return {
        "status": "deleted",
        "deleted_count": deleted_count,
        "skipped_running_count": skipped_running_count,
        "cancelled_bulk_count": cancelled_bulk_count,
    }


@app.get("/history")
def get_history(url: Optional[str] = None, limit: int = 100):
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")

    normalized_url = _normalize_url(url) if url else None

    with history_lock:
        with sqlite3.connect(HISTORY_DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT job_id, created_at, finished_at, status, urls_json, out_path,
                       download_audio, download_video, error, files_json
                FROM download_history
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    results: List[Dict[str, object]] = []
    for row in rows:
        urls = json.loads(row[4] or "[]")
        if normalized_url and normalized_url not in urls:
            continue
        results.append(
            {
                "job_id": row[0],
                "created_at": row[1],
                "finished_at": row[2],
                "status": row[3],
                "urls": urls,
                "out_path": row[5],
                "download_audio": bool(row[6]),
                "download_video": bool(row[7]),
                "error": row[8],
                "files": json.loads(row[9] or "[]"),
            }
        )

    return {"count": len(results), "items": results}


@app.get("/history/lookup")
def lookup_history(url: str):
    normalized_url = _normalize_url(url)
    history = get_history(url=normalized_url, limit=1000)
    return {
        "url": normalized_url,
        "previously_requested": history["count"] > 0,
        "items": history["items"],
    }


@app.get("/history/successes")
def lookup_successful_history(url: str):
    normalized_url = _normalize_url(url)
    history = get_history(url=normalized_url, limit=1000)
    successful_items = [item for item in history["items"] if item.get("status") == JobStatus.SUCCESS.value]
    return {
        "url": normalized_url,
        "previously_downloaded_successfully": len(successful_items) > 0,
        "count": len(successful_items),
        "items": successful_items,
    }


def _extract_job_details(job: Job) -> Dict[str, List[Dict[str, str]]]:
    audio_formats = set()
    video_streams = []

    def strip_prefix(line: str) -> str:
        if "OUT: " in line:
            return line.split("OUT: ", 1)[1]
        if "ERR: " in line:
            return line.split("ERR: ", 1)[1]
        return line

    # Look for lines like: "Downloading track 1 of 12: ... - 16-bit / 44.1 kHz ALAC"
    for raw_line in job.logs:
        line = strip_prefix(raw_line)
        if "Downloading track" in line and " - " in line:
            try:
                specs = line.split(" - ", 1)[1].strip()
                audio_formats.add(specs)
            except Exception:
                continue
        # Look for video info lines like "1000 Kbps, 1080p (1920x1080)"
        if "Kbps" in line and "(" in line and ")" in line:
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 2:
                stream_info: Dict[str, str] = {}
                if "FPS" in parts[0] and len(parts) >= 3:
                    stream_info["frame_rate"] = parts[0]
                    stream_info["kbps"] = parts[1]
                    stream_info["resolution"] = ", ".join(parts[2:])
                else:
                    stream_info["kbps"] = parts[0]
                    stream_info["resolution"] = ", ".join(parts[1:])
                video_streams.append(stream_info)

    return {
        "audio_formats": sorted(list(audio_formats)),
        "video_streams": video_streams,
    }


def _probe_media_streams(path: str) -> Dict[str, object]:
    info: Dict[str, object] = {
        "path": path,
        "exists": os.path.exists(path),
    }
    if not info["exists"]:
        return info

    info["size_bytes"] = os.path.getsize(path)
    try:
        proc = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-i", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        probe_text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    except Exception as exc:
        info["probe_error"] = str(exc)
        return info

    has_video = "Video:" in probe_text
    has_audio = "Audio:" in probe_text
    info["has_video"] = has_video
    info["has_audio"] = has_audio
    if has_video and has_audio:
        info["contains"] = "audio+video"
    elif has_video:
        info["contains"] = "video-only"
    elif has_audio:
        info["contains"] = "audio-only"
    else:
        info["contains"] = "unknown"
    return info


def _get_completed_file_report(job: Job) -> List[Dict[str, object]]:
    file_map: Dict[str, Dict[str, object]] = {}
    for event in job.file_events:
        path = str(event.get("path", ""))
        if not path:
            continue
        file_map[path] = {
            "path": path,
            "state": event.get("state"),
            "kind": event.get("kind"),
        }

    reports: List[Dict[str, object]] = []
    for path, base in file_map.items():
        if not os.path.exists(path):
            continue
        probe = _probe_media_streams(path)
        merged = dict(base)
        merged.update(probe)
        reports.append(merged)
    return reports


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = {
        "id": job.id,
        "status": job.status,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "exit_code": job.exit_code,
        "error": job.error,
        "summary": _request_summary(job.request),
    }
    if job.status == JobStatus.RUNNING and job.progress is not None:
        result["progress"] = job.progress
    if job.status in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED):
        details: Dict[str, object] = _extract_job_details(job)
        files = _get_completed_file_report(job)
        if files:
            details["files"] = files
        result["details"] = details
    return result


@app.get("/files")
def get_output_file(path: str):
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path), filename=file_path.name)


@app.post("/folders/reveal")
def reveal_output_folder(path: str):
    target_path = Path(path).expanduser().resolve()
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    system = platform.system()
    try:
        if system == "Darwin":
            cmd = ["open", "-R", str(target_path)] if target_path.is_file() else ["open", str(target_path)]
        elif system == "Windows":
            cmd = ["explorer", "/select,", str(target_path)] if target_path.is_file() else ["explorer", str(target_path)]
        else:
            opener = shutil.which("xdg-open")
            if not opener:
                raise RuntimeError("No desktop folder opener is available in this environment.")
            folder = target_path.parent if target_path.is_file() else target_path
            cmd = [opener, str(folder)]
        subprocess.Popen(cmd)
    except Exception as exc:
        raise HTTPException(status_code=501, detail=f"Could not open folder from this runtime: {exc}")

    return {"ok": True, "path": str(target_path)}


@app.post("/folders/choose")
def choose_output_folder(current: Optional[str] = None):
    system = platform.system()
    initial = Path(current or _default_out_path()).expanduser()
    if not initial.exists():
        initial = initial.parent if initial.parent.exists() else Path.home()

    try:
        if system == "Darwin":
            script = (
                'POSIX path of (choose folder with prompt "Choose Nugs output folder" '
                f'default location POSIX file "{str(initial)}")'
            )
            chosen = subprocess.check_output(["osascript", "-e", script], text=True).strip()
        elif system == "Windows":
            selected_path = str(initial).replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$d.Description = 'Choose Nugs output folder'; "
                f"$d.SelectedPath = '{selected_path}'; "
                "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
            )
            chosen = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps], text=True).strip()
        else:
            raise RuntimeError("Native folder picker is not available on this platform.")
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return {"status": "cancelled", "path": str(current or "")}
        raise HTTPException(status_code=500, detail=f"Could not choose folder: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=501, detail=f"Could not choose folder: {exc}")

    if not chosen:
        return {"status": "cancelled", "path": str(current or "")}
    return {"status": "selected", "path": chosen}


@app.post("/jobs/{job_id}/restart", status_code=201)
def restart_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in (JobStatus.RUNNING, JobStatus.PENDING):
        raise HTTPException(status_code=400, detail="Cannot restart a running or pending job")

    req_data = job.request.model_dump() if hasattr(job.request, "model_dump") else job.request.dict()  # type: ignore[attr-defined]
    req_data["download_if_already_downloaded"] = True
    req = DownloadRequest(**req_data)
    created = _enqueue_request(req)
    return {
        **created,
        "restarted_from": job_id,
    }


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.RUNNING, JobStatus.PENDING):
        raise HTTPException(status_code=400, detail="Job is not running or pending")

    with jobs_lock:
        if job.status == JobStatus.PENDING:
            # Remove from queue if pending
            try:
                pending_queue.remove(job_id)
            except ValueError:
                pass
            job.status = JobStatus.CANCELLED
            job.finished_at = datetime.utcnow()
            _history_upsert(job)
            return {"status": "cancelled"}

    _cancel_running_job(job)
    return {"status": "cancelled"}


@app.post("/jobs/{job_id}/delete")
def post_delete_job(job_id: str):
    return delete_job(job_id)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == JobStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Cannot delete a running job. Cancel it first.")

    with jobs_lock:
        try:
            pending_queue.remove(job_id)
        except ValueError:
            pass
        del jobs[job_id]
    return {"status": "deleted"}


@app.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str, lines: Optional[int] = 200):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if lines is None:
        lines = len(job.logs)
    return {"logs": list(job.logs)[-lines:]}
