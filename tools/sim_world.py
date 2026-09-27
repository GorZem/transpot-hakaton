"""Упрощённая модель улицы для оценки алгоритмов управления (tools/compare_policies.py).

В работу системы не входит: там данные идут от камер. Машины и пешеходы приходят по суточному профилю,
подчиняются сигналам светофора, иногда появляются группы, нарушители и спецтранспорт.
Выдаёт то же, что выдавало бы зрение: кто ждёт, очереди, подходящие машины, события.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from node.model import Layout, Observation, Ped, Veh

# Доля от пиковой интенсивности по часам (будний день, спальный район).
PROFILE = [0.08, 0.05, 0.04, 0.04, 0.06, 0.15, 0.45, 0.90, 1.00, 0.80, 0.65, 0.60,
           0.62, 0.62, 0.60, 0.65, 0.80, 0.95, 1.00, 0.80, 0.55, 0.40, 0.28, 0.15]


def profile(hour: float) -> float:
    h0 = int(hour) % 24
    f = hour - int(hour)
    return PROFILE[h0] * (1 - f) + PROFILE[(h0 + 1) % 24] * f


@dataclass
class Measure:
    passed: dict[str, int] = field(default_factory=dict)
    stopped: dict[str, int] = field(default_factory=dict)
    arrived: dict[str, int] = field(default_factory=dict)
    served: list[tuple[str, float, bool]] = field(default_factory=list)  # группа, ожидание, на красный
    notes: list[str] = field(default_factory=list)
    veh_queue_s: float = 0.0


class World:
    HEADWAY_S = 1.1        # разъезд очереди: 2 полосы
    START_LOSS_S = 1.5     # потерянное время на старт
    TRAVEL_S = (10.0, 16.0)

    def __init__(self, layout: Layout, veh_peak_vph: dict[str, float], ped_peak_pm: dict[str, float], seed: int = 0):
        self.L = layout
        self.rng = random.Random(seed)
        self.veh_peak = veh_peak_vph
        self.ped_peak = ped_peak_pm
        self.approach: dict[str, list[dict]] = {g: [] for g in layout.veh_groups()}
        self.queue: dict[str, list[dict]] = {g: [] for g in layout.veh_groups()}
        self.timer: dict[str, float] = {g: 0.0 for g in layout.veh_groups()}
        self.waiting: dict[str, list[tuple[float, float]]] = {g: [] for g in layout.ped_groups()}
        self.crossing: dict[str, list[float]] = {g: [] for g in layout.ped_groups()}
        self.volume = 1.0  # множитель интенсивности для демонстрации

    # ---------- демонстрационные события ----------
    def add_group(self, now: float, size: int = 10, group: str | None = None) -> str:
        g = group or self.rng.choice(self.L.ped_groups())
        for _ in range(size):
            self.waiting[g].append((now + self.rng.uniform(0, 6), self.rng.uniform(60, 140)))
        return g

    def add_emergency(self, group: str | None = None) -> str:
        g = group or self.rng.choice(self.L.veh_groups())
        self.approach[g].append({"eta": 25.0, "em": True})
        return g

    # ---------- шаг модели ----------
    def step(self, now: float, dt: float, hour: float, sig: dict[str, str]) -> Measure:
        m = Measure()
        m.veh_queue_s = sum(len(q) for q in self.queue.values()) * dt
        k = profile(hour) * self.volume
        rng = self.rng
        for g in self.L.veh_groups():
            if rng.random() < self.veh_peak.get(g, 300) * k / 3600 * dt:
                self.approach[g].append({"eta": rng.uniform(*self.TRAVEL_S), "em": False})
            s = sig.get(g)
            green = s in (Veh.GREEN, Veh.GREEN_BLINK, Veh.FLASH)  # при жёлтом мигающем едут с уступанием
            keep = []
            for v in self.approach[g]:
                v["eta"] -= dt
                if v["eta"] > 0:
                    keep.append(v)
                    continue
                committed = s == Veh.YELLOW and v["eta"] > -dt  # уже не может остановиться
                if (green or committed) and not self.queue[g]:
                    m.passed[g] = m.passed.get(g, 0) + 1
                else:
                    self.queue[g].append(v)
                    m.stopped[g] = m.stopped.get(g, 0) + 1
            self.approach[g] = keep
            if green and self.queue[g]:
                self.timer[g] += dt
                while self.queue[g] and self.timer[g] >= self.HEADWAY_S:
                    self.queue[g].pop(0)
                    self.timer[g] -= self.HEADWAY_S
                    m.passed[g] = m.passed.get(g, 0) + 1
            elif not green:
                self.timer[g] = -self.START_LOSS_S

        for g in self.L.ped_groups():
            rate = self.ped_peak.get(g, 3) * k / 60
            if rng.random() < rate * dt:
                self.waiting[g].append((now, rng.uniform(35, 110)))
                m.arrived[g] = m.arrived.get(g, 0) + 1
            if rng.random() < 0.0008 * k * dt:  # изредка группа (школьники, выход из автобуса): ~раз в 20 мин в пик
                self.add_group(now, rng.randint(5, 12), g)
                m.notes.append(f"group:{g}")
            length = self.L.groups[g].length_m or 14.0
            self.crossing[g] = [t for t in self.crossing[g] if t > now]
            arrived = [w for w in self.waiting[g] if w[0] <= now]
            if sig.get(g) == Ped.GREEN or (sig.get(g) == Ped.OFF and rng.random() < 0.2 * dt):
                for a, _ in arrived:
                    m.served.append((g, now - a, False))
                    self.crossing[g].append(now + length / rng.uniform(1.0, 1.6))
                self.waiting[g] = [w for w in self.waiting[g] if w[0] > now]
            else:
                # Нетерпеливые переходят на красный, если рядом нет машин.
                conflict = self.L.conflicting(g)
                safe = all(not self.queue.get(c) and all(v["eta"] > 5 for v in self.approach.get(c, []))
                           for c in conflict if self.L.groups[c].kind == "veh")
                keep = []
                for a, patience in self.waiting[g]:
                    if a <= now and now - a > patience and safe and rng.random() < 0.3 * dt:
                        m.served.append((g, now - a, True))
                        self.crossing[g].append(now + length / 1.7)
                    else:
                        keep.append((a, patience))
                self.waiting[g] = keep
        return m

    def observe(self, now: float) -> Observation:
        o = Observation()
        for g in self.L.ped_groups():
            arrived = [a for a, _ in self.waiting[g] if a <= now]
            o.waiting[g] = len(arrived)
            o.max_wait[g] = max((now - a for a in arrived), default=0.0)
            o.wait_sum[g] = sum(now - a for a in arrived)
            o.on_crosswalk[g] = len(self.crossing[g])
        for g in self.L.veh_groups():
            o.queue[g] = len(self.queue[g])
            o.eta[g] = min((v["eta"] for v in self.approach[g]), default=None)
            if any(v["em"] for v in self.approach[g] + self.queue[g]):
                o.emergency.add(g)
        return o
