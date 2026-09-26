FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# CPU-only torch: embedded PCs have no CUDA, and the CUDA wheels are ~2 GB
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY smartcross smartcross
COPY config/default.yaml config/default.yaml
COPY scripts scripts
# download model weights at build time, so the device works offline
RUN mkdir -p models && python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')" && mv yolo11n.pt models/
EXPOSE 8000
CMD ["python", "-m", "smartcross", "--config", "/data/config.yaml", "--port", "8000"]
