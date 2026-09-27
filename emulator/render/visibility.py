"""Зона видимости камер объектов на земле.

Сектор каждой камеры: вершина в точке опоры, направление взгляда, угол обзора с запасом, дальность.
Всё, что вне секторов, камеры не видят: там модели машин и людей скрываются (агенты продолжают
существовать в модели движения), а карта упрощается.
"""
from __future__ import annotations

import math

import numpy as np



class Visibility:
    AGENT_RANGE = 230.0     # дальше машина в кадре меньше ~7 пикселей
    DETAIL_RANGE = 170.0    # полная детализация карты (окна, деревья, разметка)
    CELL = 4.0

    def __init__(self, specs, cam_settings, hfov_deg: float):
        self.cams = []
        half = math.radians(hfov_deg / 2 + 12)  # запас на искажение и габариты
        for sp in specs:
            d = sp.target[:2] - sp.pos[:2]
            self.cams.append((float(sp.pos[0]), float(sp.pos[1]), math.atan2(d[1], d[0]), half))
        xs = [c[0] for c in self.cams]
        ys = [c[1] for c in self.cams]
        m = self.AGENT_RANGE + 10
        self.x0, self.y0 = min(xs) - m, min(ys) - m
        nx = int((max(xs) + m - self.x0) / self.CELL) + 1
        ny = int((max(ys) + m - self.y0) / self.CELL) + 1
        gx, gy = np.meshgrid(self.x0 + (np.arange(nx) + 0.5) * self.CELL, self.y0 + (np.arange(ny) + 0.5) * self.CELL)
        self.dist = np.full(gx.shape, np.inf)  # расстояние до ближайшей камеры, в сектор которой попадает клетка
        for cx, cy, az, half in self.cams:
            dx, dy = gx - cx, gy - cy
            r = np.hypot(dx, dy)
            ang = np.abs((np.arctan2(dy, dx) - az + np.pi) % (2 * np.pi) - np.pi)
            inside = (ang <= half) | (r < 8)
            self.dist = np.where(inside, np.minimum(self.dist, r), self.dist)

    def _cell(self, x: float, y: float) -> float:
        i = int((y - self.y0) / self.CELL)
        j = int((x - self.x0) / self.CELL)
        if 0 <= i < self.dist.shape[0] and 0 <= j < self.dist.shape[1]:
            return float(self.dist[i, j])
        return math.inf

    def agent_visible(self, x: float, y: float) -> bool:
        return self._cell(x, y) <= self.AGENT_RANGE

    def detailed(self, x: float, y: float) -> bool:
        return self._cell(x, y) <= self.DETAIL_RANGE

    def building_detailed(self, pts) -> bool:
        return any(self.detailed(p[0], p[1]) for p in pts)

    def coverage(self, bounds) -> float:
        """Доля участка в зоне видимости камер (для сводки)."""
        x0, y0, x1, y1 = bounds
        xs = np.linspace(x0, x1, 120)
        ys = np.linspace(y0, y1, 120)
        return float(np.mean([[self.agent_visible(x, y) for x in xs] for y in ys]))
