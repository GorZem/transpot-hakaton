"""Модель движения: машины (IDM), пешеходы, светофоры. Шаг фиксированный, время реальное."""
from __future__ import annotations

import math
import random
from collections import deque

import numpy as np

from emulator.config import Settings
from emulator.world.network import LANE_W, Connector, Crosswalk, Lane, Network
from emulator.world.routing import Router, dkey
from emulator.world.signals import SignalController

CAR_TYPES = {
    #            длина ширина высота  v0 (м/с)  доля
    "car":       (4.5, 1.8, 1.45, 14.0, 0.88),
    "bus":       (12.0, 2.5, 3.0, 11.5, 0.06),
    "truck":     (7.0, 2.4, 3.1, 11.5, 0.06),
    "emergency": (5.6, 2.1, 2.6, 16.5, 0.0),
}
# типы кузова легковых: длина, ширина, высота (совпадают с моделями в render/models.py)
CAR_STYLES = {"sedan": (4.6, 1.8, 1.45), "hatch": (4.1, 1.76, 1.5), "suv": (4.6, 1.88, 1.72)}
CAR_STYLE_WEIGHTS = {"sedan": 0.45, "hatch": 0.27, "suv": 0.28}
CAR_COLORS = [(0.85, 0.86, 0.88), (0.12, 0.13, 0.15), (0.55, 0.57, 0.6), (0.62, 0.1, 0.1), (0.15, 0.25, 0.5),
              (0.92, 0.92, 0.9), (0.3, 0.32, 0.35), (0.72, 0.62, 0.45), (0.2, 0.35, 0.25), (0.9, 0.75, 0.2)]
BUS_COLORS = [(0.95, 0.72, 0.1), (0.2, 0.55, 0.3), (0.85, 0.85, 0.85)]
CLOTHES = [(0.15, 0.18, 0.25), (0.35, 0.1, 0.1), (0.2, 0.3, 0.2), (0.6, 0.55, 0.45), (0.1, 0.1, 0.1),
           (0.5, 0.5, 0.55), (0.7, 0.3, 0.2), (0.25, 0.35, 0.55), (0.8, 0.8, 0.75), (0.45, 0.25, 0.45)]


LAT_SPEED = 1.3        # поперечная скорость при перестроении, м/с
SOLID_ZONE = 15.0      # сплошная перед стоп-линией: перестраиваться нельзя
VEH_GO = {"green", "green_blink", "flash_yellow", "off"}


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
        self.style = None
        if kind == "car":
            self.style = rng.choices(list(CAR_STYLE_WEIGHTS), weights=list(CAR_STYLE_WEIGHTS.values()))[0]
            L, W, H = CAR_STYLES[self.style]
        self.length, self.width, self.height = L, W, H
        self.v0 = v0 * rng.uniform(0.9, 1.08)
        self.v = v
        self.lane = lane
        self.s = s
        self.next_conn: Connector | None = None
        self.path: deque = deque()     # маршрут: участки улиц до выезда
        self.dest = None               # выезд
        self.visited: set = set()
        self.next_key = None
        self.target_arm = None        # куда едем на ближайшем узле
        self.turn = "straight"
        self.desired: list = []       # полосы, из которых возможен нужный манёвр
        self.lat = 0.0                # смещение при перестроении, м (вправо положительно)
        self.lc_next = 0.0
        self.yield_since = -1.0       # с какого момента уступает на перекрёстке
        self.y_dec: str | None = None
        self.stopped = False
        self.tick = -1
        self.color = (rng.choice(BUS_COLORS) if kind == "bus" else (0.95, 0.95, 0.95) if kind == "emergency"
                      else (0.75, 0.73, 0.7) if kind == "truck" else
                      (0.96, 0.8, 0.1) if rng.random() < 0.06 else rng.choice(CAR_COLORS))  # 6% — такси
        self.x = self.y = self.heading = 0.0

    def update_pose(self) -> None:
        c = self.s - self.length / 2
        poly = self.lane.poly
        if c < 0 and isinstance(self.lane, Connector):
            poly, c = self.lane.from_lane.poly, self.lane.from_lane.length + c
        x, y, h = poly.at(c)
        if self.lat:
            x += math.sin(h) * self.lat
            y -= math.cos(h) * self.lat
            h -= math.atan2(-math.copysign(LAT_SPEED, self.lat), max(self.v, 2.0))
        self.x, self.y, self.heading = x, y, h


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
        self.router = Router(net, self.rng)
        self.containers = list(net.lanes) + list(net.connectors)
        self.crossed_red = 0
        self.exited = 0      # машин проехало участок
        self.reroutes = 0    # не успели перестроиться и поехали туда, куда ведёт полоса

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

    def add_car(self, lane: Lane, kind: str, via=None) -> Car | None:
        tail = lane.cars[-1] if lane.cars else None
        L = CAR_TYPES[kind][0]
        if tail and tail.s - tail.length < L + 3:
            if kind != "emergency":
                return None
        v = CAR_TYPES[kind][3]
        if tail and tail.s - tail.length < 60:
            v = min(v, tail.v)
        car = Car(kind, lane, min(L, tail.s - tail.length - 3) if tail and kind == "emergency" else L, v, self.rng)
        lane.cars.append(car)
        self.cars.append(car)
        k = dkey(lane)
        car.visited = {k}
        if via is not None:  # скорая: через объект, дальше к выезду
            path, last = self.router.route(k, target_node=via)
            if last is None:
                lane.cars.remove(car)
                self.cars.remove(car)
                return None
            rest, car.dest = self.router.route(last, visited=set(path) | {k})
            car.path = deque(path + rest)
        else:
            path, car.dest = self.router.route(k)
            car.path = deque(path)
        self._plan(car)
        return car

    def _plan(self, car: Car) -> None:
        """На въезде в полосу: следующий участок маршрута, манёвр к нему и полосы, из которых он возможен."""
        lane = car.lane
        node = lane.to_node
        sib = lane.siblings or [lane]
        if node.kind == "end":
            car.target_arm, car.turn, car.desired, car.next_conn, car.next_key = None, "straight", list(sib), None, None
            return
        k = dkey(lane)
        trans = self.router.trans.get(k, {})
        if car.path and car.path[0] in trans and self._lane_change_cost(car)(car.path[0]) >= 120:
            car.path = deque()  # до нужной полосы не успеть перестроиться: другой маршрут
        if not car.path or car.path[0] not in trans:
            path, car.dest = self.router.route(k, dest=car.dest, visited=car.visited,
                                               first_cost=self._lane_change_cost(car))
            if not path:  # выезда не найти (не должно случаться): любой манёвр
                path = [self.rng.choice(list(trans))] if trans else []
            car.path = deque(path)
        nk = car.path[0] if car.path else None
        car.next_key = nk
        if nk is None:
            car.target_arm, car.turn, car.desired, car.next_conn = None, "straight", [lane], None
            return
        car.turn, car.target_arm = trans[nk]
        car.desired = [l for l in sib if any(dkey(c.to_lane) == nk for c in l.out)] or [lane]
        car.next_conn = self._conn_for(car)

    def _lane_change_cost(self, car: Car):
        """Штраф за первый манёвр маршрута: если до сплошной мало места для перестроений в нужную
        полосу, лучше манёвр, доступный из текущей полосы."""
        lane = car.lane
        room = lane.length - SOLID_ZONE - car.s
        mine = {dkey(c.to_lane) for c in lane.out}

        def cost(nk):
            if nk in mine:
                return 0.0
            idx = [l.index for l in (lane.siblings or [lane]) if any(dkey(c.to_lane) == nk for c in l.out)]
            if not idx:
                return 0.0
            need = min(abs(i - lane.index) for i in idx)
            per = room / need if need else 1e9
            return 0.0 if per > 60 else 15.0 if per > 30 else 120.0
        return cost

    def _conn_for(self, car: Car):
        lane = car.lane
        opts = [c for c in lane.out if car.next_key is None or dkey(c.to_lane) == car.next_key]
        if not opts:
            return None
        if len(opts) == 1:
            return opts[0]
        return min(opts, key=lambda c: (len(c.to_lane.cars), self.rng.random()))  # в более свободную полосу

    def _downstream_gap(self, car: Car) -> tuple[float, float]:
        """Расстояние до хвоста ближайшей машины впереди за пределами своего участка и её скорость."""
        c = car.lane
        dist = c.length - car.s
        if isinstance(c, Lane):
            conn = car.next_conn
            if conn is None:
                if c.to_node.kind == "end":
                    return 1e9, 0.0
                return dist + 1.0, 0.0  # полоса заканчивается или манёвр не выбран: ждать у конца
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
            if self._peds_in_path(conn):
                stop = True
            else:  # не въезжать на перекрёсток и переход, если за ними некуда встать
                tl = conn.to_lane
                t = conn.cars[-1] if conn.cars else (tl.cars[-1] if tl.cars else None)
                room = (t.s - t.length + (0 if t in conn.cars else conn.length)) if t else 1e9
                if room < conn.length + car.length + 2.5 and t.v < 1.5:
                    stop = True
                elif conn.merges and any(m.cars for m in conn.merges):
                    # слияние: в соседнем манёвре уже кто-то вливается — въезжать, только если места на двоих
                    tail = tl.cars[-1] if tl.cars else None
                    free = tail.s - tail.length if tail else 1e9
                    if free < 2 * car.length + 8 and (tail is None or tail.v < 3):
                        stop = True
        if not stop and conn.conflicts and dist > car.v * car.v / (2 * 7.0) - 0.5:
            # конфликт у самого въезда — ждать у стоп-линии; дальше — въехать и ждать внутри перед точкой
            at = self._conflict_stop(car, conn, -dist)
            # ждать внутри перекрёстка можно, только если перед точкой конфликта помещается машина
            stop = at is not None and at < car.length + 1.0
            if stop and car.yield_since >= 0 and self.t - car.yield_since > 20.0 and self._lane_go(lane):
                stop = False  # 20 с на зелёный без разрыва в потоке — проехать, иначе затор
        if not stop and dist < max(4.0, car.v * 0.8):
            conn.reserved = self.t + 0.8  # въезжаю: уступающие мне подождут
        return stop

    @staticmethod
    def _peds_in_path(conn: Connector) -> bool:
        """Пешеход на переходе у траектории машины (в пределах 3 м по ходу перехода)."""
        for cw, pos in conn.crosswalks:
            for p in cw.peds:
                if abs(float((p.pos - cw.center) @ cw.along) - pos) < 3.0:
                    return True
        return False

    def _lane_go(self, lane: Lane) -> bool:
        sc = lane.to_node.signal
        if sc is None:
            return True
        return sc.veh_state(lane.arm_in.axis if lane.to_node.kind == "junction" else "A") in VEH_GO

    def _merge_gap(self, car: Car, conn: Connector, rem: float) -> tuple[float, float]:
        """«Застёжка» на слиянии: машина, которая ближе к точке слияния на соседнем манёвре
        (или подъезжает к нему), считается лидером. rem — сколько мне осталось до точки слияния."""
        best, bv = 1e9, 0.0
        for other in conn.merges:
            cand = []
            for c in other.cars:
                cand.append((other.length - c.s, c))
            fl = other.from_lane
            for c in fl.cars:
                if c.next_conn is other:
                    if c.v > 1.0 or fl.length - c.s < 3:
                        cand.append((fl.length - c.s + other.length, c))
                    break
            for r, c in cand:
                if r < rem - 0.01 or (abs(r - rem) <= 0.01 and c.id < car.id):
                    g = rem - r - c.length
                    if g < best:
                        best, bv = g, c.v
        return best, bv

    def _conflict_stop(self, car: Car, conn: Connector, front: float) -> float | None:
        """Где на манёвре conn остановиться, чтобы не въехать в зону конфликта с другой машиной
        (None — ехать). front — положение передка на манёвре (отрицательное, пока машина до стоп-линии).
        Уступаю, если зону занимает другая машина; при равном приоритете — если она ближе к зоне;
        при её приоритете (я поворачиваю налево, она едет прямо) — если она едет к зоне или подъезжает
        к стоп-линии на зелёный."""
        stop_at = None
        for other, i_yield, (a_in, a_out), (b_in, b_out) in conn.conflicts:
            if front > a_in + 0.5:
                continue  # уже в зоне: проезжаю
            my_d = a_in - front
            blocked = False
            for c in other.cars:
                if c.s - c.length > b_out + 0.5:
                    continue  # уже покинула зону
                if c.v < 0.5 and c.s < b_in - 0.3:
                    continue  # сама стоит и ждёт перед зоной
                d = b_in - c.s
                if d < 0 or i_yield or d < my_d or (abs(d - my_d) < 0.1 and c.id < car.id):
                    blocked = True
                    break
            if not blocked and i_yield:
                if other.reserved > self.t:
                    blocked = True
                else:
                    # интервал, который водитель принимает: 3,5 с, после 15 с ожидания — 2 с
                    patient = car.yield_since < 0 or self.t - car.yield_since < 15
                    gap_s = 3.5 if patient else 2.0
                    fl = other.from_lane
                    head = next((c for c in fl.cars if c.next_conn is other), None)
                    if head is not None and head.v > 1.5 \
                            and fl.length - head.s < head.v * gap_s + 3 and self._lane_go(fl):
                        blocked = True
            if blocked:
                at = a_in - 1.0
                stop_at = at if stop_at is None else min(stop_at, at)
        if stop_at is not None:
            # не стоять в чужой зоне конфликта: иначе машины разных направлений запирают друг друга
            moved = True
            while moved:
                moved = False
                for _o, _y, (z_in, z_out), _zb in conn.conflicts:
                    if z_in - 1.0 < stop_at < z_out + 1.0 and front < z_in:
                        stop_at, moved = z_in - 1.0, True
        if stop_at is None:
            car.yield_since = -1.0
        elif car.yield_since < 0:
            car.yield_since = self.t
        elif front > 0 and self.t - car.yield_since > 8.0:
            return None  # стоит внутри перекрёстка слишком долго: завершить манёвр, чтобы не было затора
        return stop_at

    def _lane_change(self, car: Car, lane: Lane, dist: float) -> None:
        """Перестроение в полосу нужного манёвра; в заканчивающейся полосе — обязательное, «застёжкой»."""
        if car.lat or self.t < car.lc_next:
            return
        car.lc_next = self.t + 0.3
        if lane in car.desired or not car.desired:
            return
        ending = lane.ending
        if dist < SOLID_ZONE and not ending:
            return
        tgt_idx = min((l.index for l in car.desired), key=lambda i: abs(i - lane.index))
        step = 1 if tgt_idx > lane.index else -1
        sib = lane.siblings
        j = lane.index + step
        if not 0 <= j < len(sib):
            return
        nb = sib[j]
        s_new = car.s * nb.length / max(lane.length, 1e-6)
        if s_new < car.length + 0.5 or s_new > nb.length - 0.5:
            return
        leader = follower = None
        for c in nb.cars:  # по убыванию s
            if c.s > s_new:
                leader = c
            else:
                follower = c
                break
        urgent = ending or dist < 60
        front = leader.s - leader.length - s_new if leader else 1e9
        back = s_new - car.length - follower.s if follower else 1e9
        need_f = max(2.0, car.v * 0.5) if urgent else max(4.0, car.v * 1.0)
        need_b = (max(2.0, follower.v * 0.6) if urgent else max(5.0, follower.v * 1.2)) if follower else 0.0
        if front < need_f or back < need_b:
            if urgent:
                nb.merge_requests.append(car)  # попросить соседей оставить место
            return
        lane.cars.remove(car)
        k = 0
        while k < len(nb.cars) and nb.cars[k].s > s_new:
            k += 1
        nb.cars.insert(k, car)
        car.lane, car.s = nb, s_new
        car.lat = -step * LANE_W
        car.lc_next = self.t + 2.0
        car.next_conn = self._conn_for(car)

    def _move_cars(self, dt: float) -> None:
        tick = self.ticks
        requests = {}
        for lane in self.net.lanes:
            if lane.merge_requests:
                requests[id(lane)] = lane.merge_requests
                lane.merge_requests = []
        for cont in self.containers:
            cars = cont.cars
            if not cars:
                continue
            reqs = requests.get(id(cont))
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
                    mc = car.next_conn if isinstance(cont, Lane) else cont
                    if mc is not None and mc.merges:
                        rem = (cont.length - car.s + mc.length) if isinstance(cont, Lane) else (mc.length - car.s)
                        if rem < 80:
                            mg, mv = self._merge_gap(car, mc, rem)
                            if mg < 1e8:
                                acc = min(acc, idm(car.v, car.v0, mg, car.v - mv))
                    if isinstance(cont, Lane):
                        dist = cont.length - car.s
                        if car.next_conn is None and cont.out and dist < SOLID_ZONE:
                            # не успел перестроиться: едет туда, куда ведёт его полоса
                            conn = self.rng.choice(cont.out)
                            self.reroutes += 1  # маршрут пересчитается на следующем участке
                            car.target_arm, car.turn, car.next_conn = conn.arm_out, conn.turn, conn
                            car.next_key, car.path = dkey(conn.to_lane), deque()
                        if dist > -0.5 and self._must_stop(car, cont, dist):
                            acc = min(acc, idm(car.v, car.v0, dist + 1.5, car.v))
                if isinstance(cont, Connector) and cont.conflicts:
                    at = self._conflict_stop(car, cont, car.s)
                    if at is not None and at > car.s - 0.5:
                        acc = min(acc, idm(car.v, car.v0, at + 1.5 - car.s, car.v))
                if reqs:  # пропустить машину, которая встраивается из заканчивающейся или соседней полосы
                    for rq in reqs:
                        if rq.lane is not cont:
                            sp = rq.s * cont.length / max(rq.lane.length, 1e-6)
                            if 0 < sp - car.s < 45 and rq.v < car.v + 3:
                                acc = min(acc, idm(car.v, car.v0, sp - rq.length - car.s, car.v - rq.v))
                acc = max(acc, -8.0)
                car.v = max(0.0, car.v + acc * dt)
                car.s += car.v * dt
                if idx > 0:
                    ld = cars[idx - 1]
                    car.s = min(car.s, ld.s - ld.length - 0.3)
                if car.v < 0.5 and not car.stopped:
                    car.stopped = True
                if car.lat:
                    d = LAT_SPEED * dt
                    car.lat = 0.0 if abs(car.lat) <= d else car.lat - math.copysign(d, car.lat)
                if car.s >= cont.length and idx == 0:
                    self._transfer(car, cont)
                elif isinstance(cont, Lane) and len(car.desired) and cont not in car.desired:
                    self._lane_change(car, cont, cont.length - car.s)

    def _transfer(self, car: Car, cont) -> None:
        # страховка: если на следующем участке нет места, ждать в конце текущего, а не въезжать в машину
        nxt = cont.to_lane if isinstance(cont, Connector) else car.next_conn
        if nxt is not None and nxt.cars:
            tail = nxt.cars[-1]
            over = car.s - cont.length
            if tail.s - tail.length < over + 0.3:
                car.s, car.v = cont.length - 0.01, 0.0
                return
        cont.cars.remove(car)
        if isinstance(cont, Lane):
            conn = car.next_conn
            if conn is None:
                self.cars.remove(car)
                self.exited += 1
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
            k = dkey(cont.to_lane)
            car.visited.add(k)
            if car.path and car.path[0] == k:
                car.path.popleft()
            self._plan(car)

    def clear(self, cars: bool = True, peds: bool = True) -> dict:
        """Убрать машины и/или пешеходов. Генерация продолжается как обычно."""
        out = {"cars": 0, "pedestrians": 0}
        if cars:
            out["cars"] = len(self.cars)
            for c in self.containers:
                c.cars.clear()
            for l in self.net.lanes:
                l.merge_requests = []
            for c in self.net.connectors:
                c.reserved = -1.0
            self.cars.clear()
        if peds:
            out["pedestrians"] = len(self.peds)
            self.peds.clear()
            for cw in self.net.crosswalks:
                cw.peds.clear()
        return out

    def fill(self, per_km: float = 8.0) -> int:
        """Сразу расставить машины по свободным полосам (плотность — машин на километр полосы)."""
        added = 0
        for lane in self.net.lanes:
            if lane.cars or lane.length < 20:
                continue
            spacing = 1000.0 / max(per_km * self.cfg.traffic.traffic_scale, 1e-3)
            s = lane.length - self.rng.uniform(5, spacing)
            while s > 12:
                r = self.rng.random()
                kind = "bus" if r < 0.05 else "truck" if r < 0.09 else "car"
                car = Car(kind, lane, s, CAR_TYPES[kind][3] * 0.6, self.rng)
                lane.cars.append(car)
                self.cars.append(car)
                car.visited = {dkey(lane)}
                self._plan(car)
                added += 1
                s -= max(car.length + 8, spacing * self.rng.uniform(0.6, 1.4))
        return added

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
            if self.add_car(lane, "emergency", via=target):
                return True
        return False

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
