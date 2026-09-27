"""Запуск эмулятора: python -m emulator [--port 8100] [--window]."""
from __future__ import annotations

import argparse
import random
import threading
import time
import traceback


def main() -> None:
    ap = argparse.ArgumentParser(description="Эмулятор участка Люблино: камеры и светофоры для системы")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--width", type=int, default=960, help="ширина кадра камер")
    ap.add_argument("--height", type=int, default=540)
    ap.add_argument("--fps", type=float, default=10.0, help="кадров в секунду на камеру")
    ap.add_argument("--traffic", type=float, default=1.0, help="множитель интенсивности транспорта")
    ap.add_argument("--pedestrians", type=float, default=1.0, help="множитель потока пешеходов")
    ap.add_argument("--watchdog", type=float, default=20.0, help="секунд без команд до возврата к локальной программе")
    ap.add_argument("--warmup", type=float, default=180.0, help="секунд модельного времени до старта")
    ap.add_argument("--always-on", action="store_true", help="рендерить все камеры постоянно, даже без зрителей")
    ap.add_argument("--window", action="store_true", help="открыть 3D-окно с видом на участок")
    args = ap.parse_args()

    from panda3d.core import loadPrcFileData
    loadPrcFileData("", "\n".join([
        "window-type " + ("onscreen" if args.window else "offscreen"),
        "window-title Эмулятор участка Люблино",
        "win-size 1280 720",
        "audio-library-name null",
        "sync-video false",
        "framebuffer-multisample 1",
        "multisamples 4",
        "notify-level-display error",
        "notify-level-glgsg error",
    ]))
    from direct.showbase.ShowBase import ShowBase
    import uvicorn

    from emulator.config import Settings
    from emulator.render import scene
    from emulator.render.cameras import CameraManager, FrameHub
    from emulator.server.api import State, create_app
    from emulator.world.network import Network
    from emulator.world.sim import World

    s = Settings(host=args.host, port=args.port)
    s.camera.width, s.camera.height, s.camera.fps = args.width, args.height, args.fps
    s.traffic.traffic_scale, s.traffic.pedestrian_scale = args.traffic, args.pedestrians
    s.control.watchdog_s = args.watchdog

    print("==> Строю дорожную сеть из OSM")
    net = Network.load()
    print("    " + net.describe())
    world = World(net, s)
    print(f"==> Прогрев модели: {args.warmup:.0f} с")
    world.warmup(args.warmup)
    world.update_poses()

    base = ShowBase()
    base.setBackgroundColor(0.72, 0.8, 0.88, 1)
    root = base.render.attachNewNode("static")
    scene.build_static(root, net, random.Random(s.seed))
    lamps = scene.build_signals(base.render, net, scene.make_lamp_proto())
    agents = scene.AgentView(base.render, world)
    scene.setup_lights(base.render)
    hub = FrameHub()
    cams = CameraManager(base, net, s.camera, hub, always_on=args.always_on)
    from emulator.render.labels import Labeler
    cams.labeler = Labeler(world, cams.dist, s.camera)
    print(f"==> Камер: {len(cams.rigs)}, угол обзора {cams.dist.hfov_out:.0f}°, кадр {s.camera.width}×{s.camera.height}")

    if args.window:
        from emulator.render.orbit import OrbitCamera
        base.camLens.setNearFar(0.5, 5000)
        x0, y0, x1, y1 = net.bounds
        OrbitCamera(base, ((x0 + x1) / 2, (y0 + y1) / 2), yaw=20, pitch=50, dist=900,
                    objects=[(float(n.xy[0]), float(n.xy[1])) for n in net.objects.values()])
        print("    3D-окно: ЛКМ — поворот и наклон, ПКМ — сдвиг, колесо — масштаб, 1–0 — объекты, R — общий вид")

    st = State(world, net, cams, hub, s)
    app = create_app(st)
    server = uvicorn.Server(uvicorn.Config(app, host=s.host, port=s.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="http").start()

    clock = {"last": time.monotonic(), "acc": 0.0, "live": 0.0}
    frame_period = 1.0 / s.render_hz

    def tick(task):
        now = time.monotonic()
        clock["acc"] += min(0.25, now - clock["last"])
        clock["last"] = now
        try:
            with st.lock:
                while clock["acc"] >= s.sim_dt:
                    world.step(s.sim_dt)
                    clock["acc"] -= s.sim_dt
                world.update_poses()
                agents.sync(world.t)
                scene.update_lamps(lamps, world.t)
                cams.update(now, world.t)
                if now - clock["live"] > 0.2:
                    clock["live"] = now
                    st.build_live()
        except Exception:  # ошибка в одном кадре не должна останавливать эмулятор
            clock["acc"] = 0.0
            traceback.print_exc()
        # не крутить цикл быстрее render_hz
        spare = frame_period - (time.monotonic() - now)
        if spare > 0:
            time.sleep(spare)
        return task.cont

    base.taskMgr.add(tick, "sim")
    url = f"http://{s.host}:{s.port}"
    print(f"==> Эмулятор запущен: {url}")
    print(f"    карта и камеры:   {url}/")
    print(f"    API (Swagger):    {url}/docs")
    print(f"    поток камеры:     {url}/cam/<camera_id>.mjpg")
    try:
        base.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
