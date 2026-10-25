#!/usr/bin/env bash
# One-time install on ASUS Tinker Board (or any Debian/Ubuntu/Armbian Linux).
# Usage (inside the cloned repo):   bash scripts/install_tinkerboard.sh
# Safe to run again. Does NOT touch your other projects.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="nubtk-bot"
RUN_USER="${SUDO_USER:-$USER}"
cd "$APP_DIR"
echo "==> App folder: $APP_DIR   (runs as user: $RUN_USER)"

# 1) system packages (non-fatal: old TinkerOS repos may be archived)
if command -v apt-get >/dev/null 2>&1; then
  echo "==> Installing system packages (git, venv, build tools)..."
  sudo apt-get update || echo "   (apt update had problems — continuing)"
  sudo apt-get install -y git python3-venv python3-dev build-essential libffi-dev libjpeg-dev zlib1g-dev curl || \
    echo "   (some packages could not be installed — continuing)"
fi

# 2) Python >= 3.9
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)'; then
    PY="$(command -v "$c")"; break
  fi
done

if [ -d venv ] && venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
  echo "==> Existing venv OK ($(venv/bin/python --version))"
elif [ -n "$PY" ]; then
  echo "==> Using system $($PY --version)"
  rm -rf venv
  "$PY" -m venv venv
else
  echo "==> System Python is older than 3.9 → installing Python 3.11 with 'uv' (no system change)..."
  if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  rm -rf venv
  "$UV" venv --seed --python 3.11 venv
fi

echo "==> Installing Python packages (first time on ARM can take 5-15 min)..."
venv/bin/python -m pip install --upgrade pip wheel
venv/bin/python -m pip install -r requirements.txt

mkdir -p data logs backups
if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env — EDIT IT NOW:  nano $APP_DIR/.env"
fi

# 3) systemd service (auto-start on boot, auto-restart on crash / !restart / !update)
echo "==> Installing systemd service '$SERVICE'..."
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=NUBTK Discord class bot
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStartPre=/bin/sleep 5
ExecStart=$APP_DIR/venv/bin/python -u $APP_DIR/bot.py
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=3
Environment=PYTHONUNBUFFERED=1
Nice=5
MemoryMax=400M

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"

echo
echo "✅ Installed."
echo "   1) nano $APP_DIR/.env          (paste token, GUILD_ID, ADMIN_IDS)"
echo "   2) sudo systemctl start $SERVICE"
echo "   3) journalctl -u $SERVICE -f   (live log, Ctrl+C to exit)"
