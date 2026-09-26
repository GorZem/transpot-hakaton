"""Run: python -m smartcross [--config config/local.yaml] [--port 8000]"""
import os

# Before torch/OpenCV are imported: idle OpenMP workers sleep instead of spinning on the CPU.
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")

import argparse  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

import cv2  # noqa: E402
import uvicorn  # noqa: E402

from smartcross.config import ConfigStore
from smartcross.web.app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="SmartCross — умный пешеходный переход")
    ap.add_argument("--config", default="config/local.yaml",
                    help="рабочий конфиг; если его нет, создаётся копией --template")
    ap.add_argument("--template", default="config/default.yaml")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()
    cv2.setNumThreads(2)  # resize/colour conversion only; the heavy work is YOLO
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not Path(args.config).exists() and Path(args.template).exists():
        Path(args.config).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(args.template, args.config)
    app = create_app(ConfigStore(args.config))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
