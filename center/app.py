"""API центра и раздача админки."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from center.db import DB
from center.demo import generate
from center.hub import Hub
from node.model import Mode
from node.params import Params

ROOT = Path(__file__).resolve().parent.parent


class CameraBody(BaseModel):
    ok: bool


class ModeBody(BaseModel):
    mode: Mode | None = None


class GroupBody(BaseModel):
    size: int = 10


def create_app(db_path: str | None = None, sites_path: str | None = None, demo_history: bool = True,
               run_hub: bool = True) -> FastAPI:
    db = DB(db_path or str(ROOT / "data" / "center.db"))
    hub = Hub(Path(sites_path or ROOT / "data" / "sites.json"), db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if demo_history and not db.has_source("demo"):
            await asyncio.to_thread(generate, db, list(hub.sites.values()), hub.started)
        if run_hub:
            hub.start()
        yield
        if run_hub:
            await hub.stop()

    app = FastAPI(title="Умные переходы Люблино", lifespan=lifespan)
    app.state.hub = hub
    app.state.db = db

    def rt(sid: str):
        if sid not in hub.runtimes:
            raise HTTPException(404, f"объект {sid} не найден")
        return hub.runtimes[sid]

    # ---------- объекты и поиск ----------
    @app.get("/api/overview")
    def overview():
        return {"district": hub.district, "sites": hub.overview()}

    @app.get("/api/search")
    def search(q: str = ""):
        return hub.search_index.search(q)

    @app.get("/api/sites/{sid}")
    def site(sid: str):
        rt(sid)
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

    @app.get("/api/stats/summary")
    def stats_all(hours: float = 24):
        since = datetime.now() - timedelta(hours=hours)
        return {sid: db.summary(sid, since) for sid in hub.sites}

    @app.get("/api/sites/{sid}/events")
    def events(sid: str, limit: int = 50, important: bool = False):
        rt(sid)
        return db.events(sid, limit, ("warn", "critical") if important else None)

    @app.get("/api/events")
    def all_events(limit: int = 30):
        return db.events(None, limit, ("warn", "critical"))

    # ---------- настройки ----------
    @app.get("/api/params/schema")
    def params_schema():
        return Params.model_json_schema()

    @app.put("/api/sites/{sid}/params")
    def put_params(sid: str, body: dict):
        rt(sid)
        try:
            p = Params(**{**hub.runtimes[sid].p.model_dump(), **body})
        except ValidationError as e:
            raise HTTPException(422, [{"field": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in e.errors()])
        hub.set_params(sid, p)
        return p.model_dump()

    # ---------- управление ----------
    @app.post("/api/sites/{sid}/cameras/{cid}")
    def camera(sid: str, cid: str, body: CameraBody):
        r = rt(sid)
        if cid not in r.cam_ok:
            raise HTTPException(404, f"камера {cid} не найдена")
        r.set_camera(cid, body.ok)
        return r.snapshot()

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

    @app.post("/api/sites/{sid}/demo/group")
    def demo_group(sid: str, body: GroupBody):
        r = rt(sid)
        r.demo_group(max(1, min(30, body.size)))
        return r.snapshot()

    @app.post("/api/sites/{sid}/demo/emergency")
    def demo_emergency(sid: str):
        r = rt(sid)
        r.demo_emergency()
        return r.snapshot()

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
