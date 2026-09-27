"""Хаб центра: держит узлы оснащённых объектов, крутит их в реальном времени, пишет статистику и события.

В прототипе узлы работают внутри центра (режим «центр»): камеры читаются и распознаются здесь,
команды идут на контроллеры объектов по сети. В режиме «узел» каждый SiteRuntime запускается на
встраиваемом ПК у светофора, а в центр приходят только снимки состояния, статистика и события.
Объекты без оборудования видны на карте, но системой не управляются.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from center.db import DB
from center.search import SearchIndex
from node.model import Ped, Veh
from node.params import Params
from node.runtime import SiteRuntime
from node.vision.detector import VEHICLE, Detector, DetectorWorker

log = logging.getLogger("center")

SIG_BGR = {Veh.GREEN: (80, 200, 60), Veh.GREEN_BLINK: (80, 200, 60), Veh.YELLOW: (0, 190, 255),
           Veh.RED_YELLOW: (0, 190, 255), Veh.FLASH: (0, 190, 255), Veh.RED: (60, 60, 230), Ped.OFF: (140, 140, 140)}


class Hub:
    def __init__(self, sites_path: Path, db: DB, cfg: dict, dt: float = 0.1, start_io: bool = True):
        data = json.loads(Path(sites_path).read_text(encoding="utf-8"))
        self.district = data.get("district", "")
        self.sites: dict[str, dict] = {s["id"]: s for s in data["sites"]}
        self.db = db
        self.cfg = cfg
        self.dt = dt
        self.start_io = start_io
        eq = cfg.get("equipment", {})
        self.equipment_url = eq.get("base_url")
        d = cfg.get("detector", {})
        self.detector = Detector(d.get("model", "yolo11s.pt"), d.get("imgsz", 960), d.get("conf", 0.2), d.get("device", 0))
        self.worker = DetectorWorker(self.detector, d.get("fps_per_camera", 3))
        self.runtimes: dict[str, SiteRuntime] = {}
        self.camera_site: dict[str, str] = {}
        for sid, s in self.sites.items():
            if not s.get("equipped"):
                continue
            saved = db.get_params(sid)
            rt = SiteRuntime(s, Params(**saved) if saved else Params(), self.equipment_url)
            if rt.link:
                rt.link.heartbeat_s = eq.get("heartbeat_s", 5)
            zones = db.get_zones(sid)
            if zones:
                rt.set_zones(zones, log=False)
            self.runtimes[sid] = rt
            for c in s["cameras"]:
                self.camera_site[c["id"]] = sid
        self.search_index = SearchIndex(list(self.sites.values()))
        self._minute = datetime.now().replace(second=0, microsecond=0)
        self._task: asyncio.Task | None = None
        self.started = datetime.now()
        self._render_lock = threading.Lock()
        self._served: dict[str, tuple[int, float, bytes]] = {}  # камера -> последний показанный кадр

    def start(self) -> None:
        if self.start_io:
            self.worker.start()
            for rt in self.runtimes.values():
                rt.start(self.worker)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.worker.stop()
        for rt in self.runtimes.values():
            rt.stop()
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
            await asyncio.sleep(max(0.0, t0 + k * self.dt - time.monotonic()))

    def _flush(self, wall: datetime, final: bool = False) -> None:
        for sid, rt in self.runtimes.items():
            self.db.add_events(sid, rt.drain_events())
        minute = wall.replace(second=0, microsecond=0)
        if minute != self._minute or final:
            for sid, rt in self.runtimes.items():
                self.db.add_minute(sid, self._minute, rt.take_minute(), "live")
            self._minute = minute

    # ---------- данные для API ----------
    def site_info(self, sid: str) -> dict:
        s = self.sites[sid]
        rt = self.runtimes.get(sid)
        info = {k: v for k, v in s.items() if k not in ("arms",)}
        info["cameras"] = [{"id": c["id"], "title": c["title"], "hfov_deg": c["hfov_deg"],
                            "height_m": c["pose"]["h"]} for c in s.get("cameras", [])]
        info["layout"] = rt.layout_json() if rt else None
        info["params"] = rt.p.model_dump() if rt else None
        return info

    def overview(self) -> list[dict]:
        out = []
        for sid, s in self.sites.items():
            base = {k: s[k] for k in ("id", "kind", "title", "lat", "lon", "streets")}
            rt = self.runtimes.get(sid)
            if rt:
                out.append({**base, **rt.summary()})
            else:
                out.append({**base, "equipped": False, "status": "off", "mode": "none",
                            "mode_title": "Не оснащён: работает по своей программе", "stage": "—",
                            "waiting": 0, "flow_vph": 0, "cameras_ok": 0, "cameras_total": 0})
        return out

    def system(self) -> dict:
        return {"equipment_url": self.equipment_url, "detector": {
            "model": self.detector.model_name, "ready": self.worker.ready.is_set(), "error": self.worker.error,
            "ms_per_image": round(self.detector.ms_per_image, 1)}}

    def set_params(self, sid: str, p: Params) -> None:
        self.runtimes[sid].update_params(p)
        self.db.set_params(sid, p.model_dump())

    # ---------- видео с разметкой ----------
    def render_camera(self, cam_id: str, raw: bool = False) -> bytes | None:
        sid = self.camera_site.get(cam_id)
        if sid is None:
            return None
        rt = self.runtimes[sid]
        stream = rt.streams[cam_id]
        latest = stream.latest()
        if latest is None:
            return None
        if raw:  # чистый кадр для редактора зон
            ok, jpg = cv2.imencode(".jpg", latest.img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return jpg.tobytes() if ok else None
        # Рамки рисуем на том кадре, по которому шло распознавание: он отстаёт от свежего на 0,3–1 с.
        # Показ строго вперёд по времени: кадр не старше уже показанного, иначе картинка дёргается назад.
        ov = rt.perception.snapshot_overlay(cam_id)
        fr, dets = latest, []
        if ov is not None and rt.cam_fault.get(cam_id) is None and latest.ts - ov[0].ts < 1.5:
            fr, dets = ov
        with self._render_lock:
            served = self._served.get(cam_id)
            if served is not None and fr.seq <= served[0]:
                if latest.seq > served[0] and latest.ts - served[1] > 1.0:
                    fr, dets = latest, []  # распознавание отстало: вперёд, без рамок
                else:
                    return served[2]       # новее показанного пока нет
        img = fr.img.copy()
        cam = next(c for c in rt.cam_models if c.id == cam_id)
        geo = rt.perception.geo

        def poly(pts, color, thick=2):
            pts = np.asarray(pts, float)
            dense = np.concatenate([np.linspace(pts[i], pts[(i + 1) % len(pts)], 10, endpoint=False) for i in range(len(pts))])
            px, front = cam.ground_to_pixels(dense)
            if front.all():
                cv2.polylines(img, [px.astype(np.int32)], True, color, thick, cv2.LINE_AA)

        # зоны — так же, как их видит редактор: ручная разметка или автоматическая
        colors = {}
        for cw in geo.crosswalks:
            colors[("crosswalk", cw.id)] = SIG_BGR.get(rt.signals.get(cw.group), (200, 200, 200))
            colors[("wait", cw.id, 0)] = colors[("wait", cw.id, 1)] = (230, 90, 200)
        for i, ap in enumerate(geo.approaches):
            colors[("approach", i)] = SIG_BGR.get(rt.signals.get(ap.group), (200, 200, 200))
        h, w = img.shape[:2]
        for z in rt.zones_for(cam_id)[0]:
            pts = (np.array(z["points"]) * [w, h]).astype(np.int32)
            key = tuple(z["key"])
            cv2.polylines(img, [pts], True, colors.get(key, (200, 200, 200)), 2 if key[0] == "crosswalk" else 1, cv2.LINE_AA)
        for d in dets:
            x1, y1, x2, y2 = (int(v) for v in d.box)
            col = (0, 0, 255) if d.beacon else ((0, 170, 255) if d.kind == VEHICLE else (0, 220, 255))
            cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
        fault = rt.cam_fault.get(cam_id)
        if fault:
            cv2.rectangle(img, (0, img.shape[0] - 34), (img.shape[1], img.shape[0]), (40, 40, 200), -1)
            cv2.putText(img, "FAULT: " + fault.upper(), (12, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (255, 255, 255), 2, cv2.LINE_AA)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            return None
        data = jpg.tobytes()
        with self._render_lock:
            prev = self._served.get(cam_id)
            if prev is None or fr.seq > prev[0]:  # параллельный запрос мог уже отдать кадр новее
                self._served[cam_id] = (fr.seq, fr.ts, data)
            else:
                data = prev[2]
        return data
