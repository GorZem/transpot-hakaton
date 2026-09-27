"""Замер производительности: все камеры включены, сервер не запускается.

    python -m emulator.tools.bench --seconds 20
"""
from __future__ import annotations

import argparse
import threading
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--fps", type=float, default=10)
    ap.add_argument("--warmup", type=float, default=240)
    ap.add_argument("--traffic", type=float, default=1.0)
    ap.add_argument("--msaa", type=int, default=4)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--flat", action="store_true", help="без попиксельного освещения")
    args = ap.parse_args()

    from emulator.app import Emulator
    from emulator.config import Settings
    s = Settings()
    s.camera.fps = args.fps
    s.traffic.traffic_scale = args.traffic
    s.camera.msaa, s.camera.render_scale, s.camera.per_pixel_lighting = args.msaa, args.scale, not args.flat
    em = Emulator(s, window=False, always_on=True, warmup=args.warmup, log=lambda *_: None)
    lock = threading.RLock()
    ge = em.base.graphicsEngine
    for _ in range(20):  # прогрев шейдеров
        em.tick(lock)
        ge.renderFrame()
    seq0 = {cid: (em.hub.get(cid) or (0,))[0] for cid in em.cams.rigs}
    em.perf.clear()
    em.perf_frames = 0
    render_t, frames = 0.0, 0
    t_end = time.monotonic() + args.seconds
    t0 = time.monotonic()
    while time.monotonic() < t_end:
        em.tick(lock)
        a = time.perf_counter()
        ge.renderFrame()
        render_t += time.perf_counter() - a
        frames += 1
    wall = time.monotonic() - t0
    em.cams.pool.shutdown(wait=True)
    got = [(em.hub.get(cid) or (0,))[0] - seq0[cid] for cid in em.cams.rigs]
    print(f"цикл: {frames / wall:.1f} кадр/с; камеры: среднее {sum(got) / len(got) / wall:.1f} кадр/с "
          f"(мин {min(got) / wall:.1f}, макс {max(got) / wall:.1f}), цель {args.fps:g}")
    per = {k: v / max(frames, 1) * 1000 for k, v in em.perf.items()}
    print("мс на кадр цикла: " + ", ".join(f"{k} {v:.1f}" for k, v in per.items()) +
          f", рендер {render_t / frames * 1000:.1f}")
    print(f"машин {len(em.world.cars)} (показано {em.agents.shown['cars']}), "
          f"пешеходов {len(em.world.peds)} (показано {em.agents.shown['peds']}), "
          f"зона камер {em.vis.coverage(em.net.bounds) * 100:.0f}% участка")


if __name__ == "__main__":
    main()
