#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="nugs-downloader"
HOST="0.0.0.0"
PORT="8090"
PROMPT_MODE=0
SKIP_ENABLE=0
CONFIG_SOURCE=""
PYTHON_BIN=""
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Install Nugs Downloader API as a systemd service.

Usage:
  sudo ./scripts/install_linux_service.sh [options]

Options:
  --config PATH         Use an existing config.json file.
  --prompt              Prompt for credentials and write config.
  --host HOST           API bind host (default: 0.0.0.0).
  --port PORT           API bind port (default: 8090).
  --python PATH         Python executable for uvicorn.
  --service-name NAME   systemd service name (default: nugs-downloader).
  --skip-enable         Install unit only, do not enable/start.
  -h, --help            Show this help text.

Behavior:
  - If --config is omitted and ./config.json exists, it is used.
  - If no config file is available, the script prompts for credentials.
EOF
}

fail() {
  echo "Error: $*" >&2
  exit 1
}

ensure_range_1_5() {
  local value="$1"
  local label="$2"
  if ! [[ "$value" =~ ^[1-5]$ ]]; then
    fail "$label must be an integer between 1 and 5"
  fi
}

prompt_and_write_config() {
  local config_dest="$1"
  local email=""
  local password=""
  local format="4"
  local video_format="5"
  local out_path="Nugs downloads"
  local token=""
  local use_ffmpeg_env="false"
  local use_ffmpeg_input=""

  read -r -p "Nugs email: " email
  while [[ -z "$email" ]]; do
    read -r -p "Nugs email cannot be empty. Nugs email: " email
  done

  read -r -s -p "Nugs password: " password
  echo
  while [[ -z "$password" ]]; do
    read -r -s -p "Nugs password cannot be empty. Nugs password: " password
    echo
  done

  read -r -p "Audio format [4]: " format || true
  format="${format:-4}"
  ensure_range_1_5 "$format" "Audio format"

  read -r -p "Video format [5]: " video_format || true
  video_format="${video_format:-5}"
  ensure_range_1_5 "$video_format" "Video format"

  read -r -p "Output path [Nugs downloads]: " out_path || true
  out_path="${out_path:-Nugs downloads}"

  read -r -p "Token (optional): " token || true

  read -r -p "Use ffmpeg from PATH? [y/N]: " use_ffmpeg_input || true
  case "${use_ffmpeg_input,,}" in
    y|yes|true|1)
      use_ffmpeg_env="true"
      ;;
    *)
      use_ffmpeg_env="false"
      ;;
  esac

  CFG_EMAIL="$email" \
  CFG_PASSWORD="$password" \
  CFG_FORMAT="$format" \
  CFG_VIDEO_FORMAT="$video_format" \
  CFG_OUT_PATH="$out_path" \
  CFG_TOKEN="$token" \
  CFG_USE_FFMPEG_ENV="$use_ffmpeg_env" \
  "$PYTHON_BIN" - "$config_dest" <<'PY'
import json
import os
import pathlib
import sys

config = {
    "email": os.environ["CFG_EMAIL"],
    "password": os.environ["CFG_PASSWORD"],
    "format": int(os.environ["CFG_FORMAT"]),
    "videoFormat": int(os.environ["CFG_VIDEO_FORMAT"]),
    "outPath": os.environ["CFG_OUT_PATH"],
    "token": os.environ.get("CFG_TOKEN", ""),
    "useFfmpegEnvVar": os.environ["CFG_USE_FFMPEG_ENV"].lower() == "true",
}

path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || fail "--config requires a file path"
      CONFIG_SOURCE="$2"
      shift 2
      ;;
    --prompt)
      PROMPT_MODE=1
      shift
      ;;
    --host)
      [[ $# -ge 2 ]] || fail "--host requires a value"
      HOST="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || fail "--port requires a value"
      PORT="$2"
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || fail "--python requires a path"
      PYTHON_BIN="$2"
      shift 2
      ;;
    --service-name)
      [[ $# -ge 2 ]] || fail "--service-name requires a value"
      SERVICE_NAME="$2"
      shift 2
      ;;
    --skip-enable)
      SKIP_ENABLE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  fail "Please run as root (sudo)."
fi

if [[ -n "$CONFIG_SOURCE" && "$PROMPT_MODE" -eq 1 ]]; then
  fail "Use either --config or --prompt, not both."
fi

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$APP_DIR/p3venv/bin/python" ]]; then
    PYTHON_BIN="$APP_DIR/p3venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3 || true)"
  fi
fi

[[ -n "$PYTHON_BIN" ]] || fail "python3 not found. Install Python 3.10+ first."
[[ -x "$PYTHON_BIN" ]] || fail "Python executable is not executable: $PYTHON_BIN"

"$PYTHON_BIN" - <<'PY' >/dev/null 2>&1 || {
import uvicorn  # noqa: F401
PY
  fail "uvicorn is not installed for $PYTHON_BIN. Run: $PYTHON_BIN -m pip install -r requirements.txt"
}

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
  fail "--port must be numeric"
fi

CONFIG_DIR="/etc/${SERVICE_NAME}"
CONFIG_DEST="${CONFIG_DIR}/config.json"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [[ -z "$CONFIG_SOURCE" && "$PROMPT_MODE" -eq 0 && -f "$APP_DIR/config.json" ]]; then
  CONFIG_SOURCE="$APP_DIR/config.json"
fi

if [[ -n "$CONFIG_SOURCE" ]]; then
  [[ -f "$CONFIG_SOURCE" ]] || fail "Config file does not exist: $CONFIG_SOURCE"
  cp "$CONFIG_SOURCE" "$CONFIG_DEST"
  echo "Copied config from: $CONFIG_SOURCE"
elif [[ "$PROMPT_MODE" -eq 1 || ! -f "$CONFIG_DEST" ]]; then
  prompt_and_write_config "$CONFIG_DEST"
  echo "Wrote config to: $CONFIG_DEST"
else
  echo "Keeping existing config at: $CONFIG_DEST"
fi

chmod 600 "$CONFIG_DEST"

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Nugs Downloader API Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=NUGS_CONFIG_PATH=${CONFIG_DEST}
ExecStart=${PYTHON_BIN} -m uvicorn server:app --host ${HOST} --port ${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

if [[ "$SKIP_ENABLE" -eq 0 ]]; then
  systemctl enable --now "${SERVICE_NAME}.service"
  echo "Service enabled and started: ${SERVICE_NAME}.service"
  systemctl --no-pager --full status "${SERVICE_NAME}.service" | sed -n '1,25p'
else
  echo "Service file installed but not started: ${UNIT_PATH}"
  echo "Start it with: systemctl enable --now ${SERVICE_NAME}.service"
fi

echo
echo "Install complete"
echo "Service: ${SERVICE_NAME}.service"
echo "Config:  ${CONFIG_DEST}"
echo "Logs:    journalctl -u ${SERVICE_NAME}.service -f"
