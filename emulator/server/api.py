"""HTTP API эмулятора: видеопотоки камер, управление светофорами, сценарии, живые данные для карты.

Для системы эмулятор выглядит как оборудование на улице:
- камеры отдают MJPEG по HTTP (как IP-камеры): GET /cam/{camera_id}.mjpg, кадр — GET /cam/{camera_id}.jpg;
- светофорный контроллер объекта принимает команды: PUT /api/objects/{object_id}/signals.
Остальное (сценарии, «истина», карта) нужно для демонстрации и отладки.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from emulator.config import Settings
from emulator.render.cameras import FAULTS, CameraManager, FrameHub
from emulator.world.network import Network
from emulator.world.sim import World

STATIC = Path(__file__).resolve().parent / "static"
KIND_CODE = {"car": 0, "bus": 1, "truck": 2, "emergency": 3}
PED_CODE = {"approach": 0, "wait": 1, "cross": 2, "leave": 3}


@dataclass
class State:
    world: World
    net: Network
    cams: CameraManager
    hub: FrameHub
    settings: Settings
    lock: threading.RLock = field(default_factory=threading.RLock)
    live: str = "{}"
    started: float = field(default_factory=time.time)

    def build_live(self) -> None:
        w = self.world
        cars = [[c.id, round(c.x, 2), round(c.y, 2), round(c.heading, 3), KIND_CODE[c.kind]] for c in w.cars]
        peds = [[p.id, round(float(p.pos[0]), 2), round(float(p.pos[1]), 2), PED_CODE[p.state], int(p.violation)]
                for p in w.peds]
        sig = {str(nid): {"mode": sc.mode, "groups": {g: v.state for g, v in sc.groups.items()}}
               for nid, sc in w.signals.items()}
        self.live = json.dumps({"t": round(w.t, 1), "cars": cars, "peds": peds, "signals": sig},
                               separators=(",", ":"))


class SignalCommand(BaseModel):
    groups: dict[str, str] = Field(..., description="Новые состояния групп, например {\"veh\": \"yellow\"}")


class FaultBody(BaseModel):
    fault: str | None = Field(None, description="black | freeze | offline | noise | null — исправна")


class ScenarioBody(BaseModel):
    traffic_scale: float | None = Field(None, ge=0, le=4)
    pedestrian_scale: float | None = Field(None, ge=0, le=6)


class PedBody(BaseModel):
    count: int = Field(10, ge=1, le=40)
    crosswalk_id: int | None = None
    side: int | None = Field(None, ge=0, le=1)


def create_app(st: State) -> FastAPI:
    app = FastAPI(title="Эмулятор участка Люблино", version="1.0",
                  description="Видеопотоки камер и управление светофорами объектов пилота")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    net, world = st.net, st.world
    map_cache: dict = {}

    def obj_node(oid: str):
        node = net.objects.get(oid)
        if node is None:
            raise HTTPException(404, f"нет объекта {oid}")
        return node

    def obj_info(oid: str) -> dict:
        node = net.objects[oid]
        seed = net.seeds[oid]
        sc = node.signal
        return {
            "id": oid, "kind": seed.kind, "title": seed.title, "lat": seed.lat, "lon": seed.lon,
            "x": round(float(node.xy[0]), 1), "y": round(float(node.xy[1]), 1), "node_id": node.id,
            "streets": sorted({a.edge.name for a in node.arms}),
            "axes": node.axis_names,
            "signal": sc.snapshot(),
            "crosswalks": sorted({a.crosswalk.id: {"id": a.crosswalk.id, "group": a.crosswalk.group,
                                                    "street": a.edge.name}
                                  for a in node.arms if a.crosswalk is not None}.values(), key=lambda c: c["id"]),
            "cameras": [c for c in st.cams.describe(net.proj) if c["object_id"] == oid],
        }

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/info")
    def info():
        return {"name": "Эмулятор участка Люблино", "sim_time_s": round(world.t, 1),
                "uptime_s": round(time.time() - st.started), "objects": len(net.objects),
                "cameras": len(st.cams.rigs), "cars": len(world.cars), "pedestrians": len(world.peds),
                "watchdog_s": st.settings.control.watchdog_s, "camera_fps": st.settings.camera.fps}

    # ------------------------------------------------------------------ карта
    @app.get("/api/map")
    def world_map():
        if not map_cache:
            r = lambda v: round(float(v), 1)
            map_cache.update({
                "bounds": [r(v) for v in net.bounds],
                "roads": [{"pts": [[r(x), r(y)] for x, y in e.poly.p], "left": e.half_left, "right": e.half_right,
                           "name": e.name} for e in net.edges],
                "junctions": [{"x": r(n.xy[0]), "y": r(n.xy[1]), "r": r(n.radius + 5)} for n in net.nodes
                              if n.kind in ("junction", "priority")],
                "crosswalks": [{"id": c.id, "node_id": c.node.id, "group": c.group,
                                "a": [r(v) for v in c.end(0)], "b": [r(v) for v in c.end(1)]} for c in net.crosswalks],
                "buildings": [{"pts": [[r(x), r(y)] for x, y in b["pts"]], "h": b["height"]} for b in net.buildings],
                "signals": [{"node_id": n.id, "object_id": n.object_id, "x": r(n.xy[0]), "y": r(n.xy[1]),
                             "kind": n.signal.kind} for n in net.nodes if n.signal is not None],
                "cameras": [{"id": c["id"], "object_id": c["object_id"], "x": c["position"]["x"],
                             "y": c["position"]["y"], "azimuth": c["view"]["azimuth_deg"],
                             "hfov": c["calibration"]["hfov_deg"]} for c in st.cams.describe(net.proj)],
            })
        return map_cache

    @app.websocket("/ws/live")
    async def live(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_text(st.live)
                await asyncio.sleep(0.2)
        except (WebSocketDisconnect, RuntimeError):
            pass

    # ------------------------------------------------------------------ объекты и светофоры
    @app.get("/api/objects")
    def objects():
        with st.lock:
            return [obj_info(oid) for oid in net.objects]

    @app.get("/api/objects/{oid}")
    def one_object(oid: str):
        obj_node(oid)
        with st.lock:
            return obj_info(oid)

    @app.put("/api/objects/{oid}/signals")
    def set_signals(oid: str, cmd: SignalCommand):
        """Установить состояния групп светофора. Объект переходит в режим remote.
        Состояния транспорта: red, red_yellow, green, green_blink, yellow, flash_yellow, off.
        Пешеходов: red, green, green_blink, off. Конфликтующие зелёные переводят объект в жёлтый мигающий."""
        node = obj_node(oid)
        with st.lock:
            ok, msg = node.signal.command(cmd.groups, world.t)
            snap = node.signal.snapshot()
        if not ok:
            raise HTTPException(409, {"message": msg, "signal": snap})
        return {"ok": True, "message": msg, "signal": snap}

    @app.post("/api/objects/{oid}/heartbeat")
    def heartbeat(oid: str):
        node = obj_node(oid)
        with st.lock:
            node.signal.heartbeat()
            return node.signal.snapshot()

    @app.post("/api/objects/{oid}/release")
    def release(oid: str):
        """Вернуть объект к локальной программе."""
        node = obj_node(oid)
        with st.lock:
            node.signal.release(world.t)
            return node.signal.snapshot()

    @app.post("/api/objects/{oid}/flash")
    def flash(oid: str):
        node = obj_node(oid)
        with st.lock:
            node.signal.set_flash(world.t)
            node.signal._log("warn", "оператор перевёл объект в жёлтый мигающий")
            return node.signal.snapshot()

    @app.post("/api/objects/{oid}/reset")
    def reset(oid: str):
        """Снять жёлтый мигающий после конфликта и вернуть локальную программу."""
        node = obj_node(oid)
        with st.lock:
            node.signal.release(world.t)
            return node.signal.snapshot()

    @app.get("/api/objects/{oid}/truth")
    def truth(oid: str):
        """Фактическая обстановка на объекте (для проверки распознавания системой)."""
        obj_node(oid)
        with st.lock:
            return world.truth(oid)

    @app.get("/api/objects/{oid}/events")
    def object_events(oid: str, limit: int = 50):
        node = obj_node(oid)
        return [ev.__dict__ for ev in node.signal.events[-limit:]][::-1]

    @app.get("/api/events")
    def events(limit: int = 100):
        evs = [ev for sc in world.signals.values() if sc.object_id for ev in sc.events]
        evs.sort(key=lambda e: e.ts, reverse=True)
        return [ev.__dict__ for ev in evs[:limit]]

    # ------------------------------------------------------------------ сценарии
    @app.post("/api/objects/{oid}/pedestrians")
    def add_pedestrians(oid: str, body: PedBody = Body(default_factory=PedBody)):
        """Группа пешеходов подходит к переходу объекта."""
        node = obj_node(oid)
        cws = [a.crosswalk for a in node.arms if a.crosswalk is not None]
        if body.crosswalk_id is not None:
            cws = [c for c in cws if c.id == body.crosswalk_id]
            if not cws:
                raise HTTPException(404, "у объекта нет такого перехода")
        with st.lock:
            cw = world.rng.choice(cws)
            side = body.side if body.side is not None else world.rng.randrange(2)
            world.add_group(cw, side, body.count)
        return {"ok": True, "crosswalk_id": cw.id, "side": side, "count": body.count}

    @app.post("/api/objects/{oid}/emergency")
    def emergency(oid: str):
        """Скорая с маячками едет через объект."""
        obj_node(oid)
        with st.lock:
            ok = world.spawn_emergency(oid)
        if not ok:
            raise HTTPException(409, "въезд занят, повторите через пару секунд")
        return {"ok": True}

    @app.get("/api/scenario")
    def get_scenario():
        tr = st.settings.traffic
        return {"traffic_scale": tr.traffic_scale, "pedestrian_scale": tr.pedestrian_scale}

    @app.put("/api/scenario")
    def put_scenario(body: ScenarioBody):
        tr = st.settings.traffic
        if body.traffic_scale is not None:
            tr.traffic_scale = body.traffic_scale
        if body.pedestrian_scale is not None:
            tr.pedestrian_scale = body.pedestrian_scale
        return get_scenario()

    # ------------------------------------------------------------------ камеры
    @app.get("/api/cameras")
    def cameras():
        return st.cams.describe(net.proj)

    @app.get("/api/cameras/{cid}")
    def camera(cid: str):
        for c in st.cams.describe(net.proj):
            if c["id"] == cid:
                return c
        raise HTTPException(404, f"нет камеры {cid}")

    @app.post("/api/cameras/{cid}/fault")
    def camera_fault(cid: str, body: FaultBody):
        """Имитация неисправности: black — чёрный кадр, freeze — зависание, offline — нет потока, noise — помехи."""
        rig = st.cams.rigs.get(cid)
        if rig is None:
            raise HTTPException(404, f"нет камеры {cid}")
        if body.fault is not None and body.fault not in FAULTS:
            raise HTTPException(422, f"неизвестная неисправность; допустимы: {', '.join(FAULTS)} или null")
        rig.fault = body.fault
        return {"id": cid, "fault": rig.fault}

    @app.post("/api/overview")
    def overview(object_id: str = Body(..., embed=True)):
        obj_node(object_id)
        st.cams.set_overview(object_id)
        return {"object_id": object_id}

    def _rig(cid: str):
        if cid == "overview":
            return st.cams.overview
        rig = st.cams.rigs.get(cid)
        if rig is None:
            raise HTTPException(404, f"нет камеры {cid}")
        return rig

    @app.get("/cam/{cid}.jpg")
    async def snapshot(cid: str):
        rig = _rig(cid)
        if rig.fault == "offline":
            raise HTTPException(503, "камера не отвечает")
        st.hub.touch(cid)
        start = st.hub.get(cid)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3:
            f = st.hub.get(cid)
            if f and (start is None or f[0] != start[0] or time.time() - f[2] < 0.5):
                return Response(f[1], media_type="image/jpeg", headers={"Cache-Control": "no-store"})
            await asyncio.sleep(0.03)
        raise HTTPException(504, "кадр не готов")

    @app.get("/cam/{cid}/labeled")
    async def labeled(cid: str, min_px: float = 12.0, image: bool = True, include_hidden: bool = False):
        """Свежий кадр камеры и разметка на этом же кадре (для дообучения детектора).

        Рамки `bbox` = [x1, y1, x2, y2] в пикселях выходного кадра (с искажением объектива), обрезаны краем кадра.
        Классы: car, bus, truck, emergency, person. `min_px` — отбросить рамки, у которых и ширина, и высота меньше.
        `occluded_frac` — доля агента, закрытая домами или более близкими агентами (приближённо);
        агенты, полностью закрытые домами, не возвращаются. `include_hidden=true` — вернуть и почти закрытые (visible=false).
        `image=false` — без JPEG в ответе."""
        import base64
        rig = st.cams.rigs.get(cid)
        if rig is None:
            raise HTTPException(404, f"нет камеры {cid}")
        if rig.fault in ("offline", "freeze"):
            raise HTTPException(409, f"камера в неисправности {rig.fault}: разметка недоступна")
        t_req = time.time()
        st.hub.request_labels(cid)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 4:
            f = st.hub.get_labeled(cid)
            if f and f[2] >= t_req:
                seq, jpeg, ts, objs, meta = f
                objs = [o for o in objs if (include_hidden or o["visible"]) and
                        (o["bbox"][2] - o["bbox"][0] >= min_px or o["bbox"][3] - o["bbox"][1] >= min_px)]
                out = {"camera_id": cid, "object_id": rig.spec.object_id, "frame_seq": seq, "frame_ts": ts,
                       "sim_time_s": meta.get("sim_time_s"), "fault": meta.get("fault"),
                       "width": st.settings.camera.width, "height": st.settings.camera.height,
                       "calibration": {"K": st.cams.dist.K.round(3).tolist(), "D": st.cams.dist.D.tolist()},
                       "objects": objs}
                if image:
                    out["image_jpeg_base64"] = base64.b64encode(jpeg).decode()
                return out
            await asyncio.sleep(0.03)
        raise HTTPException(504, "кадр с разметкой не готов, повторите запрос")

    @app.get("/cam/{cid}.mjpg")
    async def stream(cid: str, request: Request):
        rig = _rig(cid)
        if rig.fault == "offline":
            raise HTTPException(503, "камера не отвечает")

        async def gen():
            st.hub.viewer(cid, 1)
            last = -1
            try:
                while not await request.is_disconnected():
                    if rig.fault == "offline":
                        break
                    f = st.hub.get(cid)
                    if f and f[0] != last:
                        last = f[0]
                        yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                               str(len(f[1])).encode() + b"\r\n\r\n" + f[1] + b"\r\n")
                    await asyncio.sleep(0.02)
            finally:
                st.hub.viewer(cid, -1)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    return app
