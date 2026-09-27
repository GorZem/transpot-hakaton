"""API центра и раздача админки."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from center.config import load_config
from center.db import DB
from center.hub import Hub
from node.model import Mode
from node.params import Params

ROOT = Path(__file__).resolve().parent.parent


class ModeBody(BaseModel):
    mode: Mode | None = None


def create_app(db_path: str | None = None, sites_path: str | None = None, cfg: dict | None = None,
               run_hub: bool = True, start_io: bool = True) -> FastAPI:
    cfg = cfg if cfg is not None else load_config()
    db = DB(db_path or str(ROOT / "data" / "center.db"))
    hub = Hub(Path(sites_path or ROOT / "data" / "sites.json"), db, cfg, start_io=start_io)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_hub:
            hub.start()
        yield
        if run_hub:
            await hub.stop()

    app = FastAPI(title="Умные переходы Люблино", lifespan=lifespan)
    app.state.hub = hub
    app.state.db = db

    def rt(sid: str):
        if sid not in hub.sites:
            raise HTTPException(404, f"объект {sid} не найден")
        if sid not in hub.runtimes:
            raise HTTPException(409, "объект не оснащён: системой не управляется")
        return hub.runtimes[sid]

    # ---------- объекты и поиск ----------
    @app.get("/api/overview")
    def overview():
        return {"district": hub.district, "sites": hub.overview()}

    @app.get("/api/system")
    def system():
        return hub.system()

    @app.get("/api/search")
    def search(q: str = ""):
        return hub.search_index.search(q)

    @app.get("/api/sites/{sid}")
    def site(sid: str):
        if sid not in hub.sites:
            raise HTTPException(404, f"объект {sid} не найден")
        return hub.site_info(sid)

    @app.get("/api/sites/{sid}/state")
    def state(sid: str):
        return rt(sid).snapshot()

    # ---------- статистика и события ----------
    @app.get("/api/sites/{sid}/stats")
    def stats(sid: str, hours: float = 24, bucket: int = 0):
        rt(sid)
        since = datetime.now() - timedelta(hours=hours)
        bucket = bucket or (5 if hours <= 6 else 15 if hours <= 24 else 60)
        return {"since": since.isoformat(timespec="minutes"), "bucket_min": bucket,
                "summary": db.summary(sid, since), "series": db.timeseries(sid, since, bucket),
                "wait_hist": db.wait_histogram(sid, since)}

    @app.get("/api/stats")
    def stats_any(site: str | None = None, hours: float = 24, bucket: int = 0):
        """Статистика объекта или всего участка (site не задан): показатели, ряды, пиковый час, инциденты."""
        if site:
            rt(site)
        since = datetime.now() - timedelta(hours=hours)
        bucket = bucket or (5 if hours <= 6 else 15 if hours <= 24 else 60)
        hourly = db.timeseries(site, since, 60)
        peak = max(hourly, key=lambda r: r["veh_passed"], default=None)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        names = {sid: s["title"] for sid, s in hub.sites.items()}
        by_site = [{"id": sid, "title": names[sid], **db.summary(sid, since)} for sid in hub.runtimes] if not site else []
        return {"since": since.isoformat(timespec="minutes"), "bucket_min": bucket,
                "summary": db.summary(site, since), "series": db.timeseries(site, since, bucket),
                "wait_hist": db.wait_histogram(site, since),
                "peak_hour": {"t": peak["t"], "veh_flow_vph": peak["veh_passed"]} if peak and peak["veh_passed"] else None,
                "incidents_today": db.count_events(site, today, ("warn", "critical")),
                "incidents": [{**e, "site_title": names.get(e["site"], e["site"])} for e in db.events(site, 30, ("warn", "critical"))],
                "by_site": by_site}

    @app.get("/api/stats/export.csv")
    def export_csv(site: str | None = None, hours: float = 24):
        since = datetime.now() - timedelta(hours=hours)
        fname = f"stats_{site or 'uchastok'}_{datetime.now():%Y%m%d_%H%M}.csv"
        return Response("﻿" + db.export_csv(site, since), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

    @app.get("/api/events")
    def all_events_any(limit: int = 30, site: str | None = None):
        names = {sid: s["title"] for sid, s in hub.sites.items()}
        return [{**e, "site_title": names.get(e["site"], e["site"])} for e in db.events(site, limit, ("warn", "critical", "info") if site else ("warn", "critical"))]

    @app.get("/api/sites/{sid}/events")
    def events(sid: str, limit: int = 50, important: bool = False):
        rt(sid)
        return db.events(sid, limit, ("warn", "critical") if important else None)

    # ---------- настройки ----------
    @app.get("/api/params/schema")
    def params_schema():
        return Params.model_json_schema()

    @app.put("/api/sites/{sid}/params")
    def put_params(sid: str, body: dict):
        r = rt(sid)
        try:
            p = Params(**{**r.p.model_dump(), **body})
        except ValidationError as e:
            raise HTTPException(422, [{"field": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in e.errors()])
        hub.set_params(sid, p)
        return p.model_dump()

    # ---------- управление оператора ----------
    @app.post("/api/sites/{sid}/mode")
    def mode(sid: str, body: ModeBody):
        r = rt(sid)
        r.set_forced_mode(body.mode)
        return r.snapshot()

    @app.post("/api/sites/{sid}/reset")
    def reset(sid: str):
        r = rt(sid)
        r.reset_trip()
        return r.snapshot()

    # ---------- видео с разметкой ----------
    @app.get("/video/{cam_id}.jpg")
    async def video_frame(cam_id: str):
        if cam_id not in hub.camera_site:
            raise HTTPException(404, "камера не найдена")
        jpg = await asyncio.to_thread(hub.render_camera, cam_id)
        if jpg is None:
            raise HTTPException(503, "нет кадров с камеры")
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/video/{cam_id}.mjpg")
    async def video(cam_id: str, request: Request):
        if cam_id not in hub.camera_site:
            raise HTTPException(404, "камера не найдена")

        async def gen():
            while not await request.is_disconnected():
                jpg = await asyncio.to_thread(hub.render_camera, cam_id)
                if jpg:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    # ---------- живые данные ----------
    @app.websocket("/ws/sites/{sid}")
    async def ws_site(ws: WebSocket, sid: str):
        await ws.accept()
        if sid not in hub.runtimes:
            await ws.close(code=4404)
            return
        try:
            while True:
                await ws.send_json(hub.runtimes[sid].snapshot())
                await asyncio.sleep(0.25)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.websocket("/ws/overview")
    async def ws_overview(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_json(hub.overview())
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            pass

    # ---------- админка ----------
    dist = ROOT / "admin" / "dist"
    if (dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = dist / path
            if path and f.is_file():
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app
