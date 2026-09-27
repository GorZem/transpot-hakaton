"""Запуск эмулятора: python -m emulator [--port 8100] [--window]."""
from __future__ import annotations

import argparse
import threading
import time


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
    ap.add_argument("--on-demand", action="store_true",
                    help="рендерить только камеры, которые кто-то смотрит (по умолчанию все камеры работают всегда)")
    ap.add_argument("--msaa", type=int, default=None, help="сглаживание кадров камер: 0, 2, 4 (дороже)")
    ap.add_argument("--window", action="store_true", help="открыть 3D-окно с видом на участок")
    args = ap.parse_args()

    import uvicorn

    from emulator.app import Emulator
    from emulator.config import Settings
    from emulator.server.api import State, create_app

    s = Settings(host=args.host, port=args.port)
    s.camera.width, s.camera.height, s.camera.fps = args.width, args.height, args.fps
    s.traffic.traffic_scale, s.traffic.pedestrian_scale = args.traffic, args.pedestrians
    s.control.watchdog_s = args.watchdog
    if args.msaa is not None:
        s.camera.msaa = args.msaa

    em = Emulator(s, window=args.window, always_on=not args.on_demand, warmup=args.warmup)
    st = State(em.world, em.net, em.cams, em.hub, s, emulator=em)
    app = create_app(st)
    server = uvicorn.Server(uvicorn.Config(app, host=s.host, port=s.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="http").start()

    live = {"t": 0.0}
    frame_period = 1.0 / s.render_hz

    def on_live(now: float) -> None:
        if now - live["t"] > 0.2:
            live["t"] = now
            st.build_live()

    def tick(task):
        t0 = time.monotonic()
        em.tick(st.lock, on_live)
        spare = frame_period - (time.monotonic() - t0)  # не крутить цикл быстрее render_hz
        if spare > 0:
            time.sleep(spare)
        return task.cont

    em.base.taskMgr.add(tick, "sim")
    url = f"http://{s.host}:{s.port}"
    print(f"==> Эмулятор запущен: {url}")
    print(f"    карта и камеры:   {url}/")
    print(f"    API (Swagger):    {url}/docs")
    print(f"    поток камеры:     {url}/cam/<camera_id>.mjpg")
    try:
        em.base.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
