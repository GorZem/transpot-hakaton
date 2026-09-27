"""Качество движения: наезды машин друг на друга, пропускная способность, машин на участке.

    python -m emulator.tools.traffic_check --minutes 10
Наезд — пересечение прямоугольников кузовов двух машин (с зазором 0,2 м).
"""
from __future__ import annotations

import argparse
import math
import sys
import time


def boxes_overlap(a, b, margin: float = 0.2) -> bool:
    """Пересечение двух повёрнутых прямоугольников (теорема о разделяющей оси)."""
    def corners(c):
        ca, sa = math.cos(c.heading), math.sin(c.heading)
        hl, hw = c.length / 2 - margin, c.width / 2 - margin
        return [(c.x + ca * dl - sa * dw, c.y + sa * dl + ca * dw) for dl, dw in
                ((hl, hw), (hl, -hw), (-hl, -hw), (-hl, hw))]
    pa, pb = corners(a), corners(b)
    for poly in (pa, pb):
        for i in range(4):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % 4]
            nx, ny = y2 - y1, x1 - x2
            proj_a = [nx * x + ny * y for x, y in pa]
            proj_b = [nx * x + ny * y for x, y in pb]
            if max(proj_a) < min(proj_b) or max(proj_b) < min(proj_a):
                return False
    return True


def overlaps(cars) -> int:
    grid: dict[tuple, list] = {}
    for c in cars:
        grid.setdefault((int(c.x // 12), int(c.y // 12)), []).append(c)
    n = 0
    for (gx, gy), cell in grid.items():
        near = [c for dx in (-1, 0, 1) for dy in (-1, 0, 1) for c in grid.get((gx + dx, gy + dy), ())]
        for a in cell:
            for b in near:
                if a.id < b.id and abs(a.x - b.x) < 14 and abs(a.y - b.y) < 14 and boxes_overlap(a, b):
                    n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--traffic", type=float, default=1.0)
    ap.add_argument("--warmup", type=float, default=180)
    args = ap.parse_args()
    from emulator.config import Settings
    from emulator.world.network import Network
    from emulator.world.sim import World
    s = Settings()
    s.traffic.traffic_scale = args.traffic
    w = World(Network.load(), s)
    w.warmup(args.warmup)
    exited0 = getattr(w, "exited", None)
    samples, total, worst = 0, 0, 0
    counts = []
    t0 = time.perf_counter()
    steps = int(args.minutes * 60 / s.sim_dt)
    per = int(1.0 / s.sim_dt)
    for i in range(steps):
        w.step(s.sim_dt)
        if i % per == 0:
            w.update_poses()
            o = overlaps(w.cars)
            total += o
            worst = max(worst, o)
            samples += 1
            counts.append(len(w.cars))
    wall = time.perf_counter() - t0
    extra = ""
    if exited0 is not None:
        extra = f", проехали участок {w.exited - exited0}, не успели перестроиться {getattr(w, 'reroutes', 0)}"
    print(f"трафик ×{args.traffic}: наездов в среднем {total / samples:.2f} на снимок (макс {worst}); "
          f"машин {counts[0]}→{counts[-1]} (макс {max(counts)}){extra}; шаг {wall / steps * 1000:.2f} мс")


if __name__ == "__main__":
    sys.exit(main())
