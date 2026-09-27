"""Геометрия: проекция WGS84 в локальные метры и ломаные линии.

Система координат сцены: x на восток, y на север, z вверх, метры от центра участка.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Projection:
    lat0: float
    lon0: float

    @property
    def kx(self) -> float:
        return 111_320.0 * math.cos(math.radians(self.lat0))

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * 111_320.0

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / 111_320.0, self.lon0 + x / self.kx


def unit(v: np.ndarray) -> np.ndarray:
    n = float(np.hypot(v[0], v[1]))
    return v / n if n > 1e-9 else np.array([1.0, 0.0])


def right_normal(d: np.ndarray) -> np.ndarray:
    """Нормаль вправо от направления d (правостороннее движение)."""
    return np.array([d[1], -d[0]])


def heading_deg(d: np.ndarray) -> float:
    """Азимут вектора в градусах: 0 — на север, 90 — на восток."""
    return math.degrees(math.atan2(d[0], d[1])) % 360


def angle_diff(a: float, b: float) -> float:
    """Разница углов b - a в диапазоне (-180, 180]."""
    return (b - a + 180) % 360 - 180


class Polyline:
    """Ломаная с параметризацией по длине дуги."""

    def __init__(self, pts):
        p = np.asarray(pts, dtype=float)
        keep = [0]
        for i in range(1, len(p)):
            if np.hypot(*(p[i] - p[keep[-1]])) > 0.05:
                keep.append(i)
        p = p[keep]
        if len(p) < 2:
            p = np.array([p[0], p[0] + [0.1, 0.0]])
        self.p = p
        seg = np.diff(p, axis=0)
        self.seglen = np.hypot(seg[:, 0], seg[:, 1])
        self.cum = np.concatenate([[0.0], np.cumsum(self.seglen)])
        self.length = float(self.cum[-1])
        self.dir = seg / self.seglen[:, None]

    def _idx(self, s: float) -> int:
        i = int(np.searchsorted(self.cum, s, side="right")) - 1
        return min(max(i, 0), len(self.seglen) - 1)

    def at(self, s: float) -> tuple[float, float, float]:
        """Точка и угол направления (радианы, от оси x) на расстоянии s от начала."""
        s = min(max(s, 0.0), self.length)
        i = self._idx(s)
        pt = self.p[i] + self.dir[i] * (s - self.cum[i])
        return float(pt[0]), float(pt[1]), math.atan2(self.dir[i][1], self.dir[i][0])

    def point(self, s: float) -> np.ndarray:
        x, y, _ = self.at(s)
        return np.array([x, y])

    def direction(self, s: float) -> np.ndarray:
        return self.dir[self._idx(min(max(s, 0.0), self.length))].copy()

    def sub(self, s0: float, s1: float) -> "Polyline":
        s0, s1 = max(0.0, s0), min(self.length, s1)
        if s1 - s0 < 0.2:
            mid = (s0 + s1) / 2
            return Polyline([self.point(mid - 0.1), self.point(mid + 0.1)])
        pts = [self.point(s0)]
        pts += [self.p[i] for i in range(len(self.p)) if s0 < self.cum[i] < s1]
        pts.append(self.point(s1))
        return Polyline(pts)

    def reversed(self) -> "Polyline":
        return Polyline(self.p[::-1])

    def offset(self, d: float) -> "Polyline":
        """Сдвиг вправо на d метров (влево при d < 0), с сопряжением углов."""
        n = len(self.p)
        out = []
        for i in range(n):
            if i == 0:
                nrm = right_normal(self.dir[0])
            elif i == n - 1:
                nrm = right_normal(self.dir[-1])
            else:
                a, b = right_normal(self.dir[i - 1]), right_normal(self.dir[i])
                m = unit(a + b)
                cos = max(float(np.dot(m, a)), 0.5)
                nrm = m / cos
            out.append(self.p[i] + nrm * d)
        return Polyline(out)

    def project(self, pt) -> tuple[float, float]:
        """Ближайшая точка: (s, расстояние)."""
        pt = np.asarray(pt, dtype=float)
        best = (0.0, 1e18)
        for i in range(len(self.seglen)):
            a, d, L = self.p[i], self.dir[i], self.seglen[i]
            t = min(max(float(np.dot(pt - a, d)), 0.0), L)
            dist = float(np.hypot(*(a + d * t - pt)))
            if dist < best[1]:
                best = (float(self.cum[i] + t), dist)
        return best


def bezier(p0, h0, p1, h1, n: int = 12) -> Polyline:
    """Кубическая кривая между точками с заданными направлениями (для манёвров на перекрёстке)."""
    p0, p1, h0, h1 = map(lambda v: np.asarray(v, dtype=float), (p0, p1, h0, h1))
    k = float(np.hypot(*(p1 - p0))) * 0.4
    c0, c1 = p0 + h0 * k, p1 - h1 * k
    t = np.linspace(0, 1, n)[:, None]
    pts = (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * c0 + 3 * (1 - t) * t ** 2 * c1 + t ** 3 * p1
    return Polyline(pts)
