#!/usr/bin/env bash
# Запуск SmartCross локально одной командой: окружение, зависимости, демо-видео, сервер.
# Использование: ./run.sh [--port 8000]
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
command -v ffmpeg >/dev/null || { echo "Нужен ffmpeg: sudo apt install ffmpeg"; exit 1; }

if [ ! -x .venv/bin/python ]; then
  echo "==> Создаю виртуальное окружение .venv"
  "$PY" -m venv .venv
fi

if ! .venv/bin/python -c "import torch, ultralytics, fastapi" 2>/dev/null; then
  echo "==> Ставлю зависимости (torch CPU ~200 МБ, один раз)"
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
  .venv/bin/pip install -q -r requirements-dev.txt
fi

if ! ls data/videos/*.mp4 >/dev/null 2>&1; then
  echo "==> Скачиваю демо-видео с Wikimedia Commons (~10 МБ)"
  .venv/bin/python scripts/fetch_videos.py
fi

PORT=8000
for a in "$@"; do [ "${prev:-}" = "--port" ] && PORT=$a; prev=$a; done
echo "==> Запускаю: http://localhost:${PORT}  (остановить — Ctrl+C)"
exec .venv/bin/python -m smartcross "$@"
