#!/usr/bin/env python3
"""Build clean distributable packages for Nugs Downloader.

The package is intentionally allowlisted so local credentials, job history,
tests, agent metadata, docs, and development artifacts never enter dist.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
STAGING = DIST / "staging"
APP_NAME = "Nugs Downloader"
RUNTIME_DIR_NAME = "NugsDownloader"

RUNTIME_FILES = [
    "launcher.py",
    "main.py",
    "server.py",
    "nugs_credentials.py",
    "requirements.txt",
]

WEB_FILES = [
    "index.html",
    "pot-leaf.svg",
]

ICONSET_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

DENYLIST_NAMES = {
    ".agents",
    ".codex",
    ".git",
    ".github",
    "__pycache__",
    "p3venv",
    "tests",
    "scripts",
    "docker",
    "data",
    "dist",
    "README.md",
    "token.md",
    "config.json",
    "config.example.json",
    "download_history.sqlite3",
    "nugs_credentials.enc",
    "nugs_credentials.key",
}


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def copy_runtime(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for rel in RUNTIME_FILES:
        shutil.copy2(ROOT / rel, dest / rel)

    web_dest = dest / "web"
    web_dest.mkdir(parents=True, exist_ok=True)
    for rel in WEB_FILES:
        shutil.copy2(ROOT / "web" / rel, web_dest / rel)


def write_text(path: Path, body: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(path.stat().st_mode | 0o755)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + kind + data + crc.to_bytes(4, "big")


def _write_png(path: Path, width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> None:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        row = pixels[y * width : (y + 1) * width]
        for pixel in row:
            raw.extend(pixel)
    data = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"),
            _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9)),
            _png_chunk(b"IEND", b""),
        ]
    )
    path.write_bytes(data)


def _point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i, point in enumerate(polygon):
        xi, yi = point
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            intersect_x = (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
            if x < intersect_x:
                inside = not inside
        j = i
    return inside


def _draw_polygon(pixels: list[tuple[int, int, int, int]], size: int, polygon: list[tuple[float, float]], color: tuple[int, int, int, int]) -> None:
    scaled = [(x * size, y * size) for x, y in polygon]
    min_x = max(0, int(min(x for x, _ in scaled)))
    max_x = min(size - 1, int(max(x for x, _ in scaled)) + 1)
    min_y = max(0, int(min(y for _, y in scaled)))
    max_y = min(size - 1, int(max(y for _, y in scaled)) + 1)
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            if _point_in_polygon(x + 0.5, y + 0.5, scaled):
                pixels[y * size + x] = color


def _draw_line(pixels: list[tuple[int, int, int, int]], size: int, start: tuple[float, float], end: tuple[float, float], width: float, color: tuple[int, int, int, int]) -> None:
    x1, y1 = start[0] * size, start[1] * size
    x2, y2 = end[0] * size, end[1] * size
    radius = max(1.0, width * size / 2)
    min_x = max(0, int(min(x1, x2) - radius))
    max_x = min(size - 1, int(max(x1, x2) + radius))
    min_y = max(0, int(min(y1, y2) - radius))
    max_y = min(size - 1, int(max(y1, y2) + radius))
    dx = x2 - x1
    dy = y2 - y1
    length_sq = dx * dx + dy * dy or 1.0
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            projection = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length_sq))
            px = x1 + projection * dx
            py = y1 + projection * dy
            if ((x - px) ** 2 + (y - py) ** 2) ** 0.5 <= radius:
                pixels[y * size + x] = color


def _rounded_rect_pixel(x: int, y: int, size: int, radius: float) -> bool:
    r = radius * size
    margin = 0
    left = margin
    right = size - margin - 1
    top = margin
    bottom = size - margin - 1
    cx = min(max(x, left + r), right - r)
    cy = min(max(y, top + r), bottom - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def generate_pot_leaf_png(path: Path, size: int) -> None:
    """Generate original app icon artwork; no external icon assets are used."""
    cream = (248, 245, 239, 255)
    green = (45, 143, 67, 255)
    dark_green = (30, 111, 50, 255)
    pixels = [
        cream if _rounded_rect_pixel(x, y, size, 0.22) else (0, 0, 0, 0)
        for y in range(size)
        for x in range(size)
    ]
    leaves = [
        [(0.50, 0.10), (0.40, 0.48), (0.50, 0.38), (0.60, 0.48)],
        [(0.39, 0.22), (0.21, 0.60), (0.44, 0.50)],
        [(0.61, 0.22), (0.79, 0.60), (0.56, 0.50)],
        [(0.28, 0.38), (0.10, 0.72), (0.39, 0.61)],
        [(0.72, 0.38), (0.90, 0.72), (0.61, 0.61)],
        [(0.43, 0.50), (0.33, 0.86), (0.52, 0.67)],
        [(0.57, 0.50), (0.67, 0.86), (0.48, 0.67)],
    ]
    for leaf in leaves:
        _draw_polygon(pixels, size, leaf, green)
    _draw_line(pixels, size, (0.50, 0.18), (0.50, 0.90), 0.026, dark_green)
    _draw_line(pixels, size, (0.50, 0.62), (0.30, 0.82), 0.018, dark_green)
    _draw_line(pixels, size, (0.50, 0.62), (0.70, 0.82), 0.018, dark_green)
    _write_png(path, size, size, pixels)


def generate_macos_icon(resources_dir: Path) -> Path | None:
    iconutil = shutil.which("iconutil")
    if platform.system() != "Darwin" or not iconutil:
        return None
    iconset = resources_dir / "NugsDownloader.iconset"
    reset_dir(iconset)
    for filename, size in ICONSET_SIZES:
        generate_pot_leaf_png(iconset / filename, size)
    icns_path = resources_dir / "NugsDownloader.icns"
    if icns_path.exists():
        icns_path.unlink()
    try:
        subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(icns_path)], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"Warning: macOS icon generation failed: {exc}", file=sys.stderr)
        shutil.rmtree(iconset)
        return None
    shutil.rmtree(iconset)
    return icns_path


def create_macos_runtime_venv(resources_dir: Path, app_root: Path) -> Path:
    venv_dir = resources_dir / "venv"
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    subprocess.run([sys.executable, "-m", "venv", "--copies", str(venv_dir)], check=True)
    python_bin = venv_dir / "bin" / "python"
    subprocess.run([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(python_bin), "-m", "pip", "install", "-r", str(app_root / "requirements.txt")], check=True)
    for agent_dir in venv_dir.rglob(".agents"):
        shutil.rmtree(agent_dir, ignore_errors=True)
    for cache_dir in venv_dir.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for bytecode in venv_dir.rglob("*.pyc"):
        bytecode.unlink(missing_ok=True)
    return python_bin


def build_macos_windowed_app(runtime_dir: Path, icon_path: Path | None) -> Path:
    build_venv = STAGING / "macos-build-venv"
    if build_venv.exists():
        shutil.rmtree(build_venv)
    subprocess.run([sys.executable, "-m", "venv", "--copies", str(build_venv)], check=True)
    python_bin = build_venv / "bin" / "python"
    subprocess.run([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(python_bin), "-m", "pip", "install", "-r", str(runtime_dir / "requirements.txt"), "pyinstaller"], check=True)

    dist_path = STAGING / "macos"
    work_path = STAGING / "pyinstaller-work"
    spec_path = STAGING / "pyinstaller-spec"
    for path in (dist_path, work_path, spec_path):
        reset_dir(path)

    add_data_sep = ":"
    cmd = [
        str(build_venv / "bin" / "pyinstaller"),
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(dist_path),
        "--workpath",
        str(work_path),
        "--specpath",
        str(spec_path),
        "--add-data",
        f"{runtime_dir / 'web'}{add_data_sep}web",
        "--add-data",
        f"{runtime_dir / 'main.py'}{add_data_sep}.",
        "--add-data",
        f"{runtime_dir / 'server.py'}{add_data_sep}.",
        "--add-data",
        f"{runtime_dir / 'nugs_credentials.py'}{add_data_sep}.",
        "--hidden-import",
        "webview.platforms.cocoa",
        "--hidden-import",
        "server",
        "--hidden-import",
        "main",
        "--hidden-import",
        "nugs_credentials",
        "--hidden-import",
        "sqlite3",
        "--hidden-import",
        "_sqlite3",
    ]
    if icon_path and icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])
    cmd.append("launcher.py")
    subprocess.run(cmd, cwd=runtime_dir, check=True)
    return dist_path / f"{APP_NAME}.app"


def make_windows_package() -> Path:
    package_dir = STAGING / "windows" / RUNTIME_DIR_NAME
    copy_runtime(package_dir)

    write_text(
        package_dir / "Nugs Downloader.cmd",
        r"""@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv || exit /b 1
)
for /f "usebackq delims=" %%H in (`".venv\Scripts\python.exe" -c "import hashlib; print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())"`) do set REQ_HASH=%%H
set REQ_MARKER=.venv\requirements.sha256
set SAVED_REQ_HASH=
if exist "%REQ_MARKER%" set /p SAVED_REQ_HASH=<"%REQ_MARKER%"
if not "%REQ_HASH%"=="%SAVED_REQ_HASH%" (
  ".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1
  echo %REQ_HASH%>"%REQ_MARKER%"
)
set NUGS_CONFIG_PATH=%LOCALAPPDATA%\NugsDownloader\config.json
set NUGS_CREDENTIALS_PATH=%LOCALAPPDATA%\NugsDownloader\nugs_credentials.enc
set NUGS_CREDENTIALS_KEY_PATH=%LOCALAPPDATA%\NugsDownloader\nugs_credentials.key
set NUGS_HISTORY_DB_PATH=%LOCALAPPDATA%\NugsDownloader\download_history.sqlite3
set NUGS_DEFAULT_OUT_PATH=%USERPROFILE%\Music\Nugs Downloader
if not exist "%LOCALAPPDATA%\NugsDownloader" mkdir "%LOCALAPPDATA%\NugsDownloader"
".venv\Scripts\python.exe" launcher.py
""",
    )

    write_text(
        package_dir / "build-exe.ps1",
        r"""$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (!(Test-Path ".venv\Scripts\python.exe")) {
  py -3 -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed --name "Nugs Downloader" --add-data "web;web" --add-data "main.py;." --add-data "server.py;." --add-data "nugs_credentials.py;." launcher.py
Write-Host "Built dist\Nugs Downloader.exe"
""",
    )

    zip_path = DIST / "NugsDownloader-Windows-portable.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(package_dir.rglob("*")):
            zf.write(path, path.relative_to(package_dir.parent))
    return zip_path


def make_macos_package() -> tuple[Path, Path | None]:
    runtime_dir = STAGING / "macos-runtime" / RUNTIME_DIR_NAME
    copy_runtime(runtime_dir)

    icon_resources = STAGING / "macos-icon"
    reset_dir(icon_resources)
    icon_path = generate_macos_icon(icon_resources)
    app_dir = build_macos_windowed_app(runtime_dir, icon_path)

    app_zip = DIST / "NugsDownloader-macOS-app.zip"
    if app_zip.exists():
        app_zip.unlink()
    with zipfile.ZipFile(app_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(app_dir.rglob("*")):
            zf.write(path, path.relative_to(app_dir.parent))

    dmg_path = DIST / "NugsDownloader-macOS.dmg"
    if dmg_path.exists():
        dmg_path.unlink()
    if platform.system() == "Darwin" and shutil.which("hdiutil"):
        subprocess.run(
            [
                "hdiutil",
                "create",
                "-volname",
                APP_NAME,
                "-srcfolder",
                str(app_dir),
                "-ov",
                "-format",
                "UDZO",
                str(dmg_path),
            ],
            check=True,
        )
    else:
        dmg_path = None

    return app_zip, dmg_path


def validate_package(path: Path) -> list[str]:
    hits: list[str] = []
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    else:
        names = [str(item.relative_to(path)) for item in path.rglob("*")]
    for name in names:
        parts = set(Path(name).parts)
        if parts & DENYLIST_NAMES:
            hits.append(name)
    return hits


def main() -> int:
    reset_dir(STAGING)
    DIST.mkdir(parents=True, exist_ok=True)
    windows_zip = make_windows_package()
    mac_zip, dmg = make_macos_package()

    artifacts = [windows_zip, mac_zip]
    if dmg is not None:
        artifacts.append(dmg)

    for artifact in artifacts:
        if artifact.suffix == ".zip":
            hits = validate_package(artifact)
            if hits:
                raise RuntimeError(f"Private/dev files leaked into {artifact}: {hits[:10]}")

    print("Built clean packages:")
    for artifact in artifacts:
        print(f"  {artifact}")
    print("Excluded credentials, token files, history DB, tests, scripts, docker, README, git, and agent metadata.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
