"""Хаб центра: держит узлы объектов, крутит их в реальном времени, пишет статистику и события.

В прототипе узлы работают внутри центра (режим «центр»). В режиме «узел» каждый SiteRuntime
запускается на своём встраиваемом ПК, а в центр приходят только снимки состояния, минутная
статистика и события — те же данные, что хаб сейчас берёт у локальных узлов.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from center.db import DB
from center.search import SearchIndex
from node.params import Params
from node.runtime import SiteRuntime

log = logging.getLogger("center")


class Hub:
    def __init__(self, sites_path: Path, db: DB, dt: float = 0.1):
        data = json.loads(Path(sites_path).read_text(encoding="utf-8"))
        self.district = data.get("district", "")
        self.sites: dict[str, dict] = {s["id"]: s for s in data["sites"]}
        self.db = db
        self.dt = dt
        self.runtimes: dict[str, SiteRuntime] = {}
        for sid, s in self.sites.items():
            saved = db.get_params(sid)
            params = Params(**saved) if saved else Params()
            self.runtimes[sid] = SiteRuntime(s, params)
        self.search_index = SearchIndex(list(self.sites.values()))
        self._minute = datetime.now().replace(second=0, microsecond=0)
        self._task: asyncio.Task | None = None
        self.started = datetime.now()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._flush(datetime.now(), final=True)

    async def _run(self) -> None:
        t0 = time.monotonic()
        k = 0
        last_flush = t0
        while True:
            now = k * self.dt
            wall = datetime.now()
            for rt in self.runtimes.values():
                try:
                    rt.tick(now, self.dt, wall)
                except Exception:  # сбой одного объекта не должен останавливать остальные
                    log.exception("ошибка на объекте %s", rt.id)
            k += 1
            mono = time.monotonic()
            if mono - last_flush >= 1.0:
                last_flush = mono
                self._flush(wall)
            delay = t0 + k * self.dt - time.monotonic()
            await asyncio.sleep(max(0.0, delay))

    def _flush(self, wall: datetime, final: bool = False) -> None:
        for sid, rt in self.runtimes.items():
            self.db.add_events(sid, rt.drain_events())
        minute = wall.replace(second=0, microsecond=0)
        if minute != self._minute or final:
            for sid, rt in self.runtimes.items():
                self.db.add_minute(sid, self._minute, rt.take_minute(), rt.source)
            self._minute = minute

    # ---------- данные для API ----------
    def site_info(self, sid: str) -> dict:
        s, rt = self.sites[sid], self.runtimes[sid]
        return {**s, "layout": rt.layout_json(), "params": rt.p.model_dump()}

    def overview(self) -> list[dict]:
        out = []
        for sid, s in self.sites.items():
            out.append({**{k: s[k] for k in ("id", "kind", "title", "lat", "lon", "streets")},
                        **self.runtimes[sid].summary()})
        return out

    def set_params(self, sid: str, p: Params) -> None:
        self.runtimes[sid].update_params(p)
        self.db.set_params(sid, p.model_dump())
