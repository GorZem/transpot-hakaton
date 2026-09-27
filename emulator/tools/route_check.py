"""Маршруты машин: повторные проезды, время в пути, равномерность загрузки улиц.

    python -m emulator.tools.route_check --minutes 20
Повторный проезд — машина второй раз оказалась на том же участке улицы в том же направлении.
Равномерность — разброс средней плотности (машин на км полосы) по участкам: коэффициент вариации.
"""
from __future__ import annotations

import argparse
import statistics
from collections import defaultdict


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--traffic", type=float, default=1.0)
    args = ap.parse_args()
    from emulator.config import Settings
    from emulator.world.network import Lane, Network
    from emulator.world.sim import World
    s = Settings()
    s.traffic.traffic_scale = args.traffic
    w = World(Network.load(), s)
    w.warmup(180)
    hist: dict[int, list] = defaultdict(list)
    born: dict[int, float] = {}
    trips = []
    dens = defaultdict(float)
    samples = 0
    lanes_of = defaultdict(list)
    for l in w.net.lanes:
        lanes_of[(l.edge.id, l.forward)].append(l)
    per = int(1 / s.sim_dt)
    alive_prev: set = set()
    for i in range(int(args.minutes * 60 / s.sim_dt)):
        w.step(s.sim_dt)
        if i % per:
            continue
        samples += 1
        alive = set()
        for c in w.cars:
            alive.add(c.id)
            born.setdefault(c.id, w.t)
            if isinstance(c.lane, Lane):
                k = (c.lane.edge.id, c.lane.forward)
                h = hist[c.id]
                if not h or h[-1] != k:
                    h.append(k)
        for cid in alive_prev - alive:
            trips.append(w.t - born[cid])
        alive_prev = alive
        for k, ls in lanes_of.items():
            km = ls[0].length * len(ls) / 1000
            if km > 0.03:
                dens[k] += sum(len(l.cars) for l in ls) / km
    rep = sum(1 for h in hist.values() if len(h) != len(set(h)))
    d = [v / samples for v in dens.values()]
    cv = statistics.pstdev(d) / max(statistics.mean(d), 1e-9)
    empty = sum(1 for v in d if v < 1.0) / len(d)
    print(f"трафик ×{args.traffic}: машин с повторными проездами {rep}/{len(hist)} = {100 * rep / max(len(hist), 1):.1f}%; "
          f"время в пути медиана {statistics.median(trips):.0f} с, 90% {sorted(trips)[int(len(trips) * 0.9)]:.0f} с "
          f"(поездок {len(trips)}); плотность по участкам: CV {cv:.2f}, пустых (<1 машины/км) {100 * empty:.0f}%")


if __name__ == "__main__":
    main()
