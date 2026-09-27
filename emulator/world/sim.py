"""Модель движения: машины (IDM), пешеходы, светофоры. Шаг фиксированный, время реальное."""
from __future__ import annotations

import math
import random
from collections import deque

import numpy as np

from emulator.config import Settings
from emulator.world.network import Connector, Crosswalk, Lane, Network
from emulator.world.signals import SignalController

CAR_TYPES = {
    #            длина ширина высота  v0 (м/с)  доля
    "car":       (4.5, 1.8, 1.45, 14.0, 0.88),
    "bus":       (12.0, 2.5, 3.0, 11.5, 0.06),
    "truck":     (7.0, 2.4, 3.1, 11.5, 0.06),
    "emergency": (5.6, 2.1, 2.6, 16.5, 0.0),
}
CAR_COLORS = [(0.85, 0.86, 0.88), (0.12, 0.13, 0.15), (0.55, 0.57, 0.6), (0.62, 0.1, 0.1), (0.15, 0.25, 0.5),
              (0.92, 0.92, 0.9), (0.3, 0.32, 0.35), (0.72, 0.62, 0.45), (0.2, 0.35, 0.25), (0.9, 0.75, 0.2)]
BUS_COLORS = [(0.95, 0.72, 0.1), (0.2, 0.55, 0.3), (0.85, 0.85, 0.85)]
CLOTHES = [(0.15, 0.18, 0.25), (0.35, 0.1, 0.1), (0.2, 0.3, 0.2), (0.6, 0.55, 0.45), (0.1, 0.1, 0.1),
           (0.5, 0.5, 0.55), (0.7, 0.3, 0.2), (0.25, 0.35, 0.55), (0.8, 0.8, 0.75), (0.45, 0.25, 0.45)]


def idm(v: float, v0: float, gap: float, dv: float) -> float:
    a, b, T, s0 = 1.5, 2.2, 1.3, 2.0
    ss = s0 + max(0.0, v * T + v * dv / (2 * math.sqrt(a * b)))
    return a * (1 - (v / v0) ** 4 - ((ss / gap) ** 2 if gap > 0.1 else 400))


class Car:
    _ids = 0

    def __init__(self, kind: str, lane, s: float, v: float, rng: random.Random):
        Car._ids += 1
        self.id = Car._ids
        self.kind = kind
        L, W, H, v0, _ = CAR_TYPES[kind]
        self.length, self.width, self.height = L, W, H
        self.v0 = v0 * rng.uniform(0.9, 1.08)
        self.v = v
        self.lane = lane
        self.s = s
        self.next_conn: Connector | None = None
        self.route: deque = deque()
        self.y_dec: str | None = None
        self.stopped = False
        self.tick = -1
        self.color = (rng.choice(BUS_COLORS) if kind == "bus" else (0.95, 0.95, 0.95) if kind == "emergency"
                      else (0.75, 0.73, 0.7) if kind == "truck" else rng.choice(CAR_COLORS))
        self.x = self.y = self.heading = 0.0

    def update_pose(self) -> None:
        c = self.s - self.length / 2
        poly = self.lane.poly
        if c < 0 and isinstance(self.lane, Connector):
            poly, c = self.lane.from_lane.poly, self.lane.from_lane.length + c
        self.x, self.y, self.heading = poly.at(c)


class Ped:
    _ids = 0

    def __init__(self, cw: Crosswalk, side: int, pos, rng: random.Random, group: int | None = None):
        Ped._ids += 1
        self.id = Ped._ids
        self.cw, self.side = cw, side
        self.pos = np.asarray(pos, dtype=float)
        self.heading = 0.0
        self.speed = rng.uniform(1.1, 1.55)
        self.state = "approach"
        self.path: deque = deque()
        self.wait_start = 0.0
        self.patience = rng.uniform(40, 130)
        self.group = group
        self.violation = False
        self.anim = rng.uniform(0, 6.28)
        self.moving = False
        self.height = rng.uniform(1.6, 1.88)
        self.top = rng.choice(CLOTHES)
        self.bottom = rng.choice(CLOTHES[:6])
        self.next_check = 0.0


class World:
    def __init__(self, net: Network, settings: Settings):
        self.net = net
        self.cfg = settings
        self.rng = random.Random(settings.seed)
        self.t = 0.0
        self.ticks = 0
        self.cars: list[Car] = []
        self.peds: list[Ped] = []
        self.signals: dict[int, SignalController] = {}
        for n in net.signal_nodes:
            names = {a.edge.name for a in n.arms}
            if n.kind == "crossing" or n.object_id or len(names) >= 2:
                sc = SignalController.for_node(n, settings.control.watchdog_s, offset=self.rng.uniform(0, 90))
                n.signal = sc
                self.signals[n.id] = sc
            elif n.kind == "junction":
                n.kind = "priority"  # слияние проезжих частей одной улицы — без светофора
        self.by_object = {oid: node.signal for oid, node in net.objects.items()}
        self.entries = [l for l in net.lanes if l.from_node.kind == "end"]
        self.containers = list(net.lanes) + list(net.connectors)
        self.crossed_red = 0

    # ---------------------------------------------------------------- шаг
    def step(self, dt: float) -> None:
        self.t += dt
        self.ticks += 1
        for sc in self.signals.values():
            sc.tick(dt, self.t)
        self._spawn_cars(dt)
        self._spawn_peds(dt)
        self._move_cars(dt)
        self._move_peds(dt)

    def warmup(self, seconds: float) -> None:
        for _ in range(int(seconds / self.cfg.sim_dt)):
            self.step(self.cfg.sim_dt)

    # ---------------------------------------------------------------- машины
    def _spawn_cars(self, dt: float) -> None:
        tr = self.cfg.traffic
        for lane in self.entries:
            vph = tr.vehicles_per_hour.get(lane.edge.highway, 250) * tr.traffic_scale / lane.count
            if self.rng.random() < vph / 3600 * dt:
                r = self.rng.random()
                kind = "bus" if r < 0.06 else "truck" if r < 0.1 else "car"
                self.add_car(lane, kind)

    def add_car(self, lane: Lane, kind: str, route: list | None = None) -> Car | None:
        tail = lane.cars[-1] if lane.cars else None
        L = CAR_TYPES[kind][0]
        if tail and tail.s - tail.length < L + 3:
            if kind != "emergency":
                return None
        v = CAR_TYPES[kind][3]
        if tail and tail.s - tail.length < 60:
            v = min(v, tail.v)
        car = Car(kind, lane, min(L, tail.s - tail.length - 3) if tail and kind == "emergency" else L, v, self.rng)
        if route:
            car.route = deque(route)
        lane.cars.append(car)
        self.cars.append(car)
        self._choose_next(car)
        return car

    def _choose_next(self, car: Car) -> None:
        lane = car.lane
        if car.route and car.route[0] in lane.out:
            car.next_conn = car.route.popleft()
            return
        car.route.clear()
        if not lane.out:
            car.next_conn = None
            return
        w = [{"straight": 0.62, "right": 0.2, "left": 0.18}[c.turn] for c in lane.out]
        car.next_conn = self.rng.choices(lane.out, weights=w)[0]

    def _downstream_gap(self, car: Car) -> tuple[float, float]:
        """Расстояние до хвоста ближайшей машины впереди за пределами своего участка и её скорость."""
        c = car.lane
        dist = c.length - car.s
        if isinstance(c, Lane):
            conn = car.next_conn
            if conn is None:
                return 1e9, 0.0
            if conn.cars:
                t = conn.cars[-1]
                return dist + t.s - t.length, t.v
            dist += conn.length
            nxt = conn.to_lane
        else:
            nxt = c.to_lane
        if nxt.cars:
            t = nxt.cars[-1]
            return dist + t.s - t.length, t.v
        return 1e9, 0.0

    def _must_stop(self, car: Car, lane: Lane, dist: float) -> bool:
        node = lane.to_node
        conn = car.next_conn
        if node.kind == "end" or conn is None:
            return False
        stop = False
        sc = node.signal
        if sc is not None and car.kind != "emergency":
            st = sc.veh_state(lane.arm_in.axis if node.kind == "junction" else "A")
            if st == "yellow":
                if car.y_dec is None:
                    car.y_dec = "go" if dist < car.v * car.v / (2 * 3.5) + 1.0 else "stop"
                stop = car.y_dec == "stop"
            elif st in ("red", "red_yellow"):
                stop = car.y_dec != "go"
            else:
                car.y_dec = None
        if not stop and dist > car.v * car.v / (2 * 6.0):
            if any(cw.peds for cw in conn.crosswalks):
                stop = True
            else:  # не въезжать на перекрёсток и переход, если за ними некуда встать
                tl = conn.to_lane
                t = conn.cars[-1] if conn.cars else (tl.cars[-1] if tl.cars else None)
                room = (t.s - t.length + (0 if t in conn.cars else conn.length)) if t else 1e9
                if room < conn.length + car.length + 2.5 and t.v < 1.5:
                    stop = True
        return stop

    def _move_cars(self, dt: float) -> None:
        tick = self.ticks
        for cont in self.containers:
            cars = cont.cars
            if not cars:
                continue
            for car in list(cars):
                if car.tick == tick or car.lane is not cont:
                    continue
                car.tick = tick
                idx = cars.index(car)
                if idx > 0:
                    ld = cars[idx - 1]
                    acc = idm(car.v, car.v0, ld.s - ld.length - car.s, car.v - ld.v)
                else:
                    gap, lv = self._downstream_gap(car)
                    acc = idm(car.v, car.v0, gap, car.v - lv) if gap < 1e8 else idm(car.v, car.v0, 1e9, 0)
                    if isinstance(cont, Lane):
                        dist = cont.length - car.s
                        if dist > -0.5 and self._must_stop(car, cont, dist):
                            acc = min(acc, idm(car.v, car.v0, dist + 1.5, car.v))
                acc = max(acc, -8.0)
                car.v = max(0.0, car.v + acc * dt)
                car.s += car.v * dt
                if idx > 0:
                    ld = cars[idx - 1]
                    car.s = min(car.s, ld.s - ld.length - 0.3)
                if car.v < 0.5 and not car.stopped:
                    car.stopped = True
                if car.s >= cont.length and idx == 0:
                    self._transfer(car, cont)

    def _transfer(self, car: Car, cont) -> None:
        cont.cars.remove(car)
        if isinstance(cont, Lane):
            conn = car.next_conn
            if conn is None:
                self.cars.remove(car)
                return
            car.s -= cont.length
            car.lane = conn
            conn.cars.append(car)
        else:
            car.s -= cont.length
            car.lane = cont.to_lane
            car.y_dec = None
            car.stopped = False
            cont.to_lane.cars.append(car)
            self._choose_next(car)

    # ---------------------------------------------------------------- пешеходы
    def _spawn_peds(self, dt: float) -> None:
        tr = self.cfg.traffic
        for cw in self.net.crosswalks:
            rate = tr.pedestrians_per_min if cw.node.object_id else tr.other_pedestrians_per_min
            if self.rng.random() < rate * tr.pedestrian_scale / 60 * dt:
                self.add_ped(cw, self.rng.randrange(2))

    def _sidewalk_dir(self, cw: Crosswalk) -> np.ndarray:
        if cw.arm is not None:
            return cw.road_dir  # от перекрёстка вдоль рукава
        return cw.road_dir * (1 if self.rng.random() < 0.5 else -1)

    def add_ped(self, cw: Crosswalk, side: int, near: bool = False, group: int | None = None) -> Ped:
        curb = cw.end(side)
        out = -cw.along if side == 0 else cw.along
        lateral = cw.road_dir * self.rng.uniform(-1.5, 1.5)
        wait = curb + out * self.rng.uniform(0.6, 2.4) + lateral
        along = self._sidewalk_dir(cw)
        dist = self.rng.uniform(3, 9) if near else self.rng.uniform(10, 38)
        start = curb + out * self.rng.uniform(1.0, 3.2) + along * dist
        p = Ped(cw, side, start, self.rng, group)
        p.path.append(wait)
        self.peds.append(p)
        return p

    def add_group(self, cw: Crosswalk, side: int, size: int) -> None:
        gid = self.rng.randrange(1_000_000)
        for _ in range(size):
            p = self.add_ped(cw, side, near=True, group=gid)
            p.speed = self.rng.uniform(1.0, 1.3)
            p.patience = 1e9

    def _road_clear(self, cw: Crosswalk) -> bool:
        c = cw.center
        for car in self.cars:
            dx, dy = c[0] - car.x, c[1] - car.y
            d2 = dx * dx + dy * dy
            if d2 < 64:
                return False
            if d2 < 1600 and car.v > 1 and dx * math.cos(car.heading) + dy * math.sin(car.heading) > 0:
                return False
        return True

    def _ped_may_go(self, p: Ped) -> tuple[bool, bool]:
        """(можно идти, это нарушение)."""
        sc = p.cw.node.signal
        st = sc.ped_state(p.cw.group) if sc is not None else "off"
        if st == "green":
            return True, False
        if st == "off":  # нерегулируемый переход или светофор выключен: идём, когда дорога свободна
            if self.t < p.next_check:
                return False, False
            p.next_check = self.t + 0.7
            return self._road_clear(p.cw), False
        if self.t - p.wait_start > p.patience and self.t >= p.next_check:
            p.next_check = self.t + 0.7
            return self._road_clear(p.cw), True
        return False, False

    def _move_peds(self, dt: float) -> None:
        for p in list(self.peds):
            p.moving = False
            if p.state == "wait":
                go, viol = self._ped_may_go(p)
                if go:
                    self._start_cross(p, viol)
                continue
            if not p.path:
                continue
            tgt = p.path[0]
            d = tgt - p.pos
            dist = float(np.hypot(d[0], d[1]))
            spd = p.speed * (1.25 if p.violation and p.state == "cross" else 1.0)
            if dist < 0.05 or dist <= spd * dt:
                p.pos = tgt.copy()
                p.path.popleft()
                if not p.path:
                    self._arrive(p)
            else:
                p.pos = p.pos + d / dist * spd * dt
                p.heading = math.atan2(d[1], d[0])
                p.moving = True
                p.anim += dt * spd * 5.5

    def _arrive(self, p: Ped) -> None:
        if p.state == "approach":
            p.state = "wait"
            p.wait_start = self.t
            p.heading = math.atan2(*(p.cw.along * (1 if p.side == 0 else -1))[::-1])
        elif p.state == "cross":
            p.cw.peds.remove(p)
            p.state = "leave"
            along = self._sidewalk_dir(p.cw)
            p.path.append(p.pos + along * self.rng.uniform(12, 35))
        elif p.state == "leave":
            self.peds.remove(p)

    def _start_cross(self, p: Ped, violation: bool) -> None:
        other = 1 - p.side
        curb = p.cw.end(other)
        out = p.cw.along if other == 1 else -p.cw.along
        lat = float(np.dot(p.pos - p.cw.center, p.cw.road_dir))
        lat = max(-1.6, min(1.6, lat))
        p.path.append(curb + out * self.rng.uniform(0.6, 1.8) + p.cw.road_dir * lat)
        p.state = "cross"
        p.violation = violation
        p.cw.peds.append(p)
        if violation:
            self.crossed_red += 1

    # ---------------------------------------------------------------- события сценариев
    def spawn_emergency(self, object_id: str) -> bool:
        target = self.net.objects[object_id]
        starts = list(self.entries)
        self.rng.shuffle(starts)
        for lane in starts:
            route = self._route(lane, target)
            if route is not None and self.add_car(lane, "emergency", route):
                return True
        return False

    def _route(self, lane: Lane, target) -> list | None:
        prev = {lane: None}
        q = deque([lane])
        while q:
            cur = q.popleft()
            if cur.to_node is target:
                path, x = [], cur
                while prev[x] is not None:
                    conn, x = prev[x]
                    path.append(conn)
                path.reverse()
                if cur.out:
                    path.append(self.rng.choice([c for c in cur.out if c.turn == "straight"] or cur.out))
                return path
            for c in cur.out:
                if c.to_lane not in prev:
                    prev[c.to_lane] = (c, cur)
                    q.append(c.to_lane)
        return None

    def update_poses(self) -> None:
        for c in self.cars:
            c.update_pose()

    # ---------------------------------------------------------------- «истина» для проверки распознавания
    def truth(self, object_id: str) -> dict:
        node = self.net.objects[object_id]
        cws = [a.crosswalk for a in node.arms if a.crosswalk is not None]
        cws = list(dict.fromkeys(cws))
        out = {"object_id": object_id, "sim_time_s": round(self.t, 1), "crosswalks": [], "approaches": []}
        for cw in cws:
            waiting = [0, 0]
            for p in self.peds:
                if p.cw is cw and p.state == "wait":
                    waiting[p.side] += 1
            out["crosswalks"].append({"id": cw.id, "group": cw.group, "waiting_side_0": waiting[0],
                                      "waiting_side_1": waiting[1], "crossing": len(cw.peds)})
        for arm in node.arms:
            lanes = arm.lanes_in()
            near = [c for l in lanes for c in l.cars if l.length - c.s < 80]
            out["approaches"].append({
                "street": arm.edge.name, "bearing_deg": round(arm.bearing), "axis": arm.axis,
                "vehicles_within_80m": len(near), "queue": sum(1 for c in near if c.v < 1.0),
                "emergency": any(c.kind == "emergency" for c in near),
            })
        return out
