"""Настройки центра из config/center.yaml и config/local.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None) -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "center.yaml").read_text(encoding="utf-8")) or {}
    local = Path(path) if path else ROOT / "config" / "local.yaml"
    if local.exists():
        cfg = _merge(cfg, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    return cfg
