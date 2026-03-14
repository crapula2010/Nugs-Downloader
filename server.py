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
import re
import sqlite3
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from subprocess import PIPE, Popen
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# --- Config ----------------------------------------------------------------

WORKDIR = Path(__file__).resolve().parent
WEB_INDEX_PATH = WORKDIR / "web" / "index.html"

# Use the same Python interpreter that runs this server (typically the virtualenv)
import sys
PYTHON = sys.executable
DOWNLOADER_SCRIPT = WORKDIR / "main.py"
HISTORY_DB_PATH = WORKDIR / "download_history.sqlite3"
FFMPEG_BIN = str((WORKDIR / "ffmpeg").resolve()) if (WORKDIR / "ffmpeg").exists() else "ffmpeg"


class ConfigResponse(BaseModel):
    max_concurrent_jobs: int
    pending_jobs: int
    running_jobs: int


class ConfigUpdate(BaseModel):
    max_concurrent_jobs: int = Field(..., gt=0)

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

# Concurrency / queue support
max_concurrent_jobs = 2
pending_queue: deque[str] = deque()


def _normalize_url(url: str) -> str:
    # strip the "/watch" prefix if present
    return url.replace("/watch/", "/")


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
    cmd = [PYTHON, "-u", str(DOWNLOADER_SCRIPT)]
    if req.format is not None:
        cmd += ["-f", str(req.format)]
    if req.video_format is not None:
        cmd += ["-F", str(req.video_format)]
    if req.out_path:
        cmd += ["-o", req.out_path]

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
                job.error = f"Exit code {proc.returncode}"
        _history_upsert(job)
    finally:
        _dispatch_next_jobs()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def web_ui() -> str:
    if not WEB_INDEX_PATH.exists():
        raise HTTPException(status_code=404, detail="Web UI file not found")
    return WEB_INDEX_PATH.read_text(encoding="utf-8")


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

    job_id = str(uuid.uuid4())
    job = Job(id=job_id, request=req)
    should_start = False

    with jobs_lock:
        jobs[job_id] = job
        if _running_job_count_unlocked() < max_concurrent_jobs:
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
        "skipped_urls": skipped_urls,
    }


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
            }
            for j in jobs.values()
        ]


@app.get("/config")
def get_config():
    return ConfigResponse(
        max_concurrent_jobs=max_concurrent_jobs,
        pending_jobs=len(pending_queue),
        running_jobs=_count_running_jobs(),
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

    # If running, attempt to terminate the process.
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