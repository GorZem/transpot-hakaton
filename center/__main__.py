"""Запуск центра: python -m center [--port 8000] [--host 127.0.0.1]"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from center.app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Центр управления умными светофорами")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=None, help="путь к базе SQLite (по умолчанию data/center.db)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
