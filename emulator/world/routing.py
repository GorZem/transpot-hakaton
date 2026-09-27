"""Маршруты машин: от въезда до выезда за границу участка, без повторного проезда улиц.

Граф: вершина — участок улицы в одном направлении (ребро + направление), переход — манёвр на узле.
Маршрут — кратчайший по времени путь до случайно выбранного выезда. Чтобы машины не ехали одним
путём и улицы заполнялись равномерно, время проезда каждого участка умножается на случайный
коэффициент (свой для каждого маршрута) и растёт с загрузкой участка.
"""
from __future__ import annotations

import heapq
import math
import random
from collections import defaultdict


def dkey(lane) -> tuple[int, bool]:
    """Участок улицы в одном направлении."""
    return lane.edge.id, lane.forward


class Router:
    SPEED = 11.0                                                   # м/с для оценки времени
    TURN_COST = {"straight": 0.0, "right": 5.0, "left": 25.0}      # с: налево дольше из-за уступания
    LOAD_COST = 6.0                                                # с на каждую машину на полосе участка
    EXIT_TAU = 60.0                                                # с: дальние выезды выбираются реже
    REVISIT_COST = 400.0                                           # с: штраф за уже проеханный участок

    def __init__(self, net, rng: random.Random):
        self.net, self.rng = net, rng
        self.lanes: dict[tuple, list] = defaultdict(list)
        for l in net.lanes:
            self.lanes[dkey(l)].append(l)
        self.length = {k: max(l.length for l in ls) for k, ls in self.lanes.items()}
        self.to_node = {k: ls[0].to_node for k, ls in self.lanes.items()}
        self.trans: dict[tuple, dict] = defaultdict(dict)  # участок -> {следующий: (манёвр, рукав)}
        for c in net.connectors:
            self.trans[dkey(c.from_lane)].setdefault(dkey(c.to_lane), (c.turn, c.arm_out))
        self.exits = [k for k in self.lanes if self.to_node[k].kind == "end"]
        self.exit_weight = {k: len(self.lanes[k]) for k in self.exits}
        self._free: dict = {}  # время в свободном движении от участка до выездов

    def load(self, k) -> float:
        ls = self.lanes[k]
        return sum(len(l.cars) for l in ls) / len(ls)

    def choose_exit(self, start) -> tuple | None:
        free = self._free.get(start)
        if free is None:
            free = self._free[start] = self._times(start)
        opts = [k for k in self.exits if k[0] != start[0] and k in free]  # не разворачиваться на той же улице
        if not opts:
            return None
        base = min(free[k] for k in opts)
        w = [self.exit_weight[k] * math.exp(-(free[k] - base) / self.EXIT_TAU) * self.rng.uniform(0.6, 1.4)
             for k in opts]
        return self.rng.choices(opts, weights=w)[0]

    def _times(self, start) -> dict:
        """Время свободного проезда от участка до всех остальных (без случайности и загрузки)."""
        dist = {start: 0.0}
        heap = [(0.0, 0, start)]
        n = 0
        while heap:
            d, _, k = heapq.heappop(heap)
            if d > dist[k]:
                continue
            for nk, (turn, _a) in self.trans.get(k, {}).items():
                nd = d + self.length[nk] / self.SPEED + self.TURN_COST[turn]
                if nd < dist.get(nk, 1e18):
                    dist[nk] = nd
                    n += 1
                    heapq.heappush(heap, (nd, n, nk))
        return dist

    def route(self, start, dest=None, visited=(), target_node=None, first_cost=None) -> tuple[list, tuple | None]:
        """Путь (список участков после start) и выезд. target_node — проехать через этот узел
        (для скорой): путь заканчивается на участке, который подходит к узлу."""
        if target_node is None and dest is None:
            dest = self.choose_exit(start)
            if dest is None:
                return [], None
        noise: dict = {}
        dist = {start: 0.0}
        prev: dict = {}
        heap = [(0.0, 0, start)]
        n = 0
        visited = set(visited)
        goal = None
        while heap:
            d, _, k = heapq.heappop(heap)
            if d > dist.get(k, 1e18):
                continue
            if (target_node is not None and self.to_node[k] is target_node and k != start) or \
                    (target_node is None and k == dest):
                goal = k
                break
            for nk, (turn, _arm) in self.trans.get(k, {}).items():
                if nk not in noise:
                    noise[nk] = self.rng.uniform(0.8, 1.4)
                c = (self.length[nk] / self.SPEED * noise[nk] + self.TURN_COST[turn]
                     + self.LOAD_COST * self.load(nk) + (self.REVISIT_COST if nk in visited else 0.0))
                if k == start and first_cost is not None:
                    c += first_cost(nk)  # первый манёвр: сколько стоит перестроиться под него
                nd = d + c
                if nd < dist.get(nk, 1e18):
                    dist[nk] = nd
                    prev[nk] = k
                    n += 1
                    heapq.heappush(heap, (nd, n, nk))
        if goal is None:
            if target_node is None and dest is not None:
                # до выбранного выезда не доехать — к ближайшему достижимому
                reach = [k for k in self.exits if k in dist and k != start]
                if not reach:
                    return [], None
                goal = min(reach, key=dist.get)
                dest = goal
            else:
                return [], None
        path = []
        k = goal
        while k != start:
            path.append(k)
            k = prev[k]
        path.reverse()
        return path, (dest if target_node is None else goal)
