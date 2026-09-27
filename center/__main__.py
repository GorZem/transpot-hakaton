"""Запуск центра: python -m center [--port 8000] [--config config/local.yaml]"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from center.app import create_app
from center.config import load_config


def main() -> None:
    ap = argparse.ArgumentParser(description="Центр управления умными светофорами")
    ap.add_argument("--config", default=None, help="дополнительный файл настроек поверх config/center.yaml")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--db", default=None, help="путь к базе SQLite (по умолчанию data/center.db)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    srv = cfg.get("server", {})
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(args.db, cfg=cfg), host=args.host or srv.get("host", "127.0.0.1"),
                port=args.port or srv.get("port", 8000), log_level="info")


if __name__ == "__main__":
    main()
