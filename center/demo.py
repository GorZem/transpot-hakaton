"""Демонстрационная история статистики за прошлые дни.

Центр в прототипе работает недавно, а графикам за неделю нужны данные. История строится по тому
же суточному профилю, что и тестовый источник, и хранится с source = 'demo'. В админке такие
данные помечены как демонстрационные.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from center.db import DB, minute_key
from node.model import build_layout
from node.runtime import _seed, default_demand
from node.synthetic import profile

WAIT_BASE = {"crossing": 13.0, "tee": 20.0, "cross": 13.0}
STOP_BASE = {"crossing": 0.48, "tee": 0.72, "cross": 0.70}


def _poisson(r: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(r.gauss(lam, lam ** 0.5)))
    k, p, limit = 0, 1.0, pow(2.718281828, -lam)
    while True:
        p *= r.random()
        if p < limit:
            return k
        k += 1


def generate(db: DB, sites: list[dict], until: datetime, days: int = 7) -> int:
    rows = []
    start = (until - timedelta(days=days)).replace(second=0, microsecond=0)
    for site in sites:
        layout = build_layout(site["kind"], site.get("road_bearing_deg", 0.0))
        veh, ped = default_demand(layout, site, _seed(site["id"]))
        r = random.Random(_seed(site["id"]) + 1)
        veh_pm, ped_pm = sum(veh.values()) / 60, sum(ped.values())
        kind = site["kind"]
        # Пара эпизодов отказа камеры за неделю, чтобы в статистике были аварийные режимы.
        faults = [start + timedelta(minutes=r.randint(0, days * 1440 - 120)) for _ in range(2)]
        t = start
        while t < until:
            weekend = t.weekday() >= 5
            k = profile(t.hour + t.minute / 60 + (1.5 if weekend else 0)) * (0.75 if weekend else 1.0)
            k *= r.uniform(0.85, 1.15)
            v = _poisson(r, veh_pm * k)
            stopped = sum(1 for _ in range(v) if r.random() < STOP_BASE[kind] * (0.6 + 0.4 * k))
            served = _poisson(r, ped_pm * k)
            wait_avg = max(2.0, r.gauss(WAIT_BASE[kind] * (0.55 + 0.6 * k), 3.0))
            wait_sum = wait_avg * served
            wait_max = min(75.0, wait_avg * r.uniform(1.6, 3.2)) if served else 0.0
            viol = sum(1 for _ in range(served) if r.random() < 0.004)
            groups = 1 if r.random() < 0.012 * k * len(ped) else 0
            phases = min(served, _poisson(r, (1.4 if kind == "crossing" else 3.5) * min(1.0, 0.3 + k)))
            degraded = any(f <= t < f + timedelta(minutes=45) for f in faults)
            modes = (0.0, 60.0, 0.0, 0.0) if degraded else (60.0, 0.0, 0.0, 0.0)
            rows.append((site["id"], minute_key(t), "demo", served, served, wait_sum, wait_max, viol, groups, phases,
                         v, stopped, *modes))
            t += timedelta(minutes=1)
    db.add_rows(rows)
    return len(rows)
