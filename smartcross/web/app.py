"""Web interface: monitoring, configuration and statistics (FastAPI)."""
from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from smartcross.config import Config, ConfigStore
from smartcross.controller.fsm import Mode
from smartcross.engine import Engine
from smartcross.stats.db import StatsDB

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
security = HTTPBasic(auto_error=False)


def require_admin(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Write endpoints are protected when SMARTCROSS_ADMIN_PASSWORD is set."""
    pwd = os.environ.get("SMARTCROSS_ADMIN_PASSWORD")
    if not pwd:
        return
    if creds is None or not (secrets.compare_digest(creds.username, "admin")
                             and secrets.compare_digest(creds.password, pwd)):
        raise HTTPException(401, "Нужна авторизация", headers={"WWW-Authenticate": "Basic"})


class FaultBody(BaseModel):
    fault: str | None = None


class ModeBody(BaseModel):
    mode: Mode | None = None


def create_app(store: ConfigStore, engine: Engine | None = None, db: StatsDB | None = None,
               start_engine: bool = True) -> FastAPI:
    db = db or StatsDB(store.cfg.database_url)
    engine = engine or Engine(store, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_engine:
            engine.start()
        yield
        if start_engine:
            engine.stop()

    app = FastAPI(title="SmartCross — умный пешеходный переход", lifespan=lifespan)
    app.state.engine, app.state.store, app.state.db = engine, store, db
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # ------------------------------------------------------------------ pages
    def page(name: str):
        async def handler(request: Request):
            return templates.TemplateResponse(request, name, {"site": store.cfg.site.name, "page": name})
        return handler

    app.get("/", response_class=HTMLResponse)(page("dashboard.html"))
    app.get("/config", response_class=HTMLResponse)(page("config.html"))
    app.get("/stats", response_class=HTMLResponse)(page("stats.html"))

    # ------------------------------------------------------------------ live
    @app.get("/api/state")
    async def state():
        return engine.get_snapshot()

    @app.get("/api/cameras/{cid}/snapshot.jpg")
    async def snapshot(cid: str, raw: bool = False):
        w = engine.workers.get(cid)
        jpg = w.jpeg(raw=raw) if w else None
        if jpg is None:
            raise HTTPException(404, "нет кадра")
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/cameras/{cid}/stream.mjpg")
    async def stream(cid: str, request: Request):
        if cid not in engine.workers:
            raise HTTPException(404, "камера не найдена")

        async def gen():
            last = None
            while not await request.is_disconnected():
                w = engine.workers.get(cid)
                jpg = w.jpeg() if w else None
                if jpg is not None and jpg is not last:
                    last = jpg
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                await asyncio.sleep(0.1)
        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    # ------------------------------------------------------------------ operator
    @app.post("/api/cameras/{cid}/fault", dependencies=[Depends(require_admin)])
    async def fault(cid: str, body: FaultBody):
        try:
            engine.inject_fault(cid, body.fault)
        except KeyError:
            raise HTTPException(404, "камера не найдена")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/mode", dependencies=[Depends(require_admin)])
    async def set_mode(body: ModeBody):
        engine.set_forced_mode(body.mode)
        return {"ok": True}

    @app.post("/api/button")
    async def button():
        engine.press_button()
        return {"ok": True}

    @app.post("/api/conflict/reset", dependencies=[Depends(require_admin)])
    async def reset_conflict():
        engine.reset_conflict()
        return {"ok": True}

    # ------------------------------------------------------------------ config
    @app.get("/api/config")
    async def get_config():
        return store.cfg.model_dump(mode="json")

    @app.get("/api/config/schema")
    async def get_schema():
        return Config.model_json_schema()

    @app.put("/api/config", dependencies=[Depends(require_admin)])
    async def put_config(request: Request):
        try:
            cfg = Config.model_validate(await request.json())
        except ValidationError as e:
            raise HTTPException(422, [{"loc": ".".join(map(str, err["loc"])), "msg": err["msg"]}
                                      for err in e.errors()])
        ids = [c.id for c in cfg.cameras]
        if len(ids) != len(set(ids)):
            raise HTTPException(422, [{"loc": "cameras", "msg": "идентификаторы камер должны быть уникальны"}])
        store.update(cfg)
        return {"ok": True}

    # ------------------------------------------------------------------ stats
    def since(hours: float) -> datetime:
        return datetime.now() - timedelta(hours=hours)

    @app.get("/api/stats/summary")
    async def summary(hours: float = 24):
        db.flush()
        return db.summary(since(hours))

    @app.get("/api/stats/timeseries")
    async def timeseries(hours: float = 24, bucket: int = 0):
        bucket = bucket or (1 if hours <= 2 else 15 if hours <= 48 else 60)
        return db.timeseries(since(hours), bucket)

    @app.get("/api/stats/classes")
    async def classes(hours: float = 24):
        return db.class_breakdown(since(hours))

    @app.get("/api/stats/waits")
    async def waits(hours: float = 24):
        return db.wait_histogram(since(hours))

    @app.get("/api/stats/export.csv")
    async def export(hours: float = 24):
        return PlainTextResponse(db.export_csv(since(hours)), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=traffic.csv"})

    @app.get("/api/events")
    async def get_events(limit: int = 50):
        return db.recent_events(limit)

    @app.get("/api/phases")
    async def get_phases(limit: int = 30):
        return db.recent_phases(limit)

    return app
