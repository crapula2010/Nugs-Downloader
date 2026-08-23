#!/usr/bin/env python3
"""Desktop launcher for the local Nugs Downloader web app."""

from __future__ import annotations

import os
import json
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn


SMOKE_ENV = "NUGS_LAUNCHER_SMOKE"
WORKER_ARG = "--nugs-worker"
HOST = "127.0.0.1"
EXTRA_FFMPEG_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")


def _state_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "NugsDownloader"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NugsDownloader"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nugs-downloader"


def configure_state() -> None:
    app_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    os.chdir(app_root)
    path_parts = [*EXTRA_FFMPEG_PATHS, os.environ.get("PATH", "")]
    os.environ["PATH"] = os.pathsep.join(part for part in path_parts if part)
    base = _state_dir()
    base.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("NUGS_CONFIG_PATH", str(base / "config.json"))
    os.environ.setdefault("NUGS_CREDENTIALS_PATH", str(base / "nugs_credentials.enc"))
    os.environ.setdefault("NUGS_CREDENTIALS_KEY_PATH", str(base / "nugs_credentials.key"))
    os.environ.setdefault("NUGS_HISTORY_DB_PATH", str(base / "download_history.sqlite3"))
    os.environ.setdefault("NUGS_DEFAULT_OUT_PATH", str(Path.home() / "Music" / "Nugs Downloader"))


def destroy_saved_login() -> None:
    for env_name in ("NUGS_CREDENTIALS_PATH", "NUGS_CREDENTIALS_KEY_PATH"):
        raw_path = os.environ.get(env_name, "").strip()
        if raw_path:
            Path(raw_path).expanduser().unlink(missing_ok=True)

    raw_config_path = os.environ.get("NUGS_CONFIG_PATH", "").strip()
    if not raw_config_path:
        return
    config_path = Path(raw_config_path).expanduser()
    if not config_path.exists():
        return
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return
    changed = False
    for key in ("email", "password", "token"):
        if key in config:
            config.pop(key, None)
            changed = True
    if changed:
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def find_free_port(host: str = HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wait_for_server(port: int, host: str = HOST, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.15)
    raise RuntimeError("Timed out waiting for Nugs Downloader to start")


def run_server(port: int) -> None:
    uvicorn.run("server:app", host=HOST, port=port, log_level="warning")


def open_window(port: int) -> None:
    try:
        import webview
    except Exception as exc:
        raise RuntimeError(
            "Standalone WebView is unavailable. Install the platform WebView "
            "dependencies from requirements.txt and restart Nugs Downloader."
        ) from exc

    webview.create_window(
        title="Nugs Downloader",
        url=f"http://{HOST}:{port}/",
        width=1480,
        height=980,
        min_size=(1100, 740),
        text_select=True,
    )
    webview.start(debug=False)


def main() -> int:
    configure_state()
    if WORKER_ARG in sys.argv:
        sys.argv.remove(WORKER_ARG)
        import main as downloader_main

        downloader_main.main()
        return 0

    destroy_saved_login()
    if os.environ.get(SMOKE_ENV) == "1":
        import webview  # noqa: F401

        print("launcher smoke ok")
        return 0
    port = find_free_port()
    server_thread = threading.Thread(target=run_server, args=(port,), daemon=True)
    server_thread.start()
    wait_for_server(port)
    try:
        open_window(port)
    finally:
        destroy_saved_login()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
