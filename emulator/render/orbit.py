"""Орбитальная камера 3D-окна: поворот вокруг вертикали и наклон, без завала горизонта.

Мышь: левая кнопка — поворот и наклон, правая или средняя — сдвиг по земле, колесо — приближение.
Клавиши: 1–9, 0 — перейти к объекту пилота, R — общий вид.
Наклон ограничен, поэтому камера всегда смотрит сверху и земля остаётся внизу кадра.
"""
from __future__ import annotations

import math

from panda3d.core import Point3

PITCH_MIN, PITCH_MAX = 8.0, 89.0      # угол взгляда вниз от горизонта, градусы
DIST_MIN, DIST_MAX = 15.0, 2500.0


class OrbitCamera:
    def __init__(self, base, target: tuple[float, float], yaw: float = 30.0, pitch: float = 45.0,
                 dist: float = 600.0, objects: list[tuple[float, float]] | None = None):
        self.base = base
        self.home = (target, yaw, pitch, dist)
        self.tx, self.ty = target
        self.yaw, self.pitch, self.dist = yaw, pitch, dist
        self.mode: str | None = None
        self.last: tuple[float, float] | None = None
        self.objects = objects or []
        base.disableMouse()
        for btn, mode in (("mouse1", "rotate"), ("mouse2", "pan"), ("mouse3", "pan")):
            base.accept(btn, self._press, [mode])
            base.accept(btn + "-up", self._release)
        base.accept("wheel_up", self.zoom, [0.87])
        base.accept("wheel_down", self.zoom, [1 / 0.87])
        base.accept("r", self.reset)
        for i in range(min(10, len(self.objects))):
            base.accept(str((i + 1) % 10), self.focus, [i])
        base.taskMgr.add(self._task, "orbit-camera", sort=-10)
        self.apply()

    # ------------------------------------------------------------ состояние
    def rotate(self, d_yaw: float, d_pitch: float) -> None:
        self.yaw = (self.yaw + d_yaw) % 360
        self.pitch = min(PITCH_MAX, max(PITCH_MIN, self.pitch + d_pitch))
        self.apply()

    def pan(self, dx: float, dy: float) -> None:
        """Сдвиг цели по земле; dx вправо, dy вперёд по направлению взгляда (в долях расстояния)."""
        y = math.radians(self.yaw)
        right = (math.cos(y), -math.sin(y))
        fwd = (math.sin(y), math.cos(y))
        k = self.dist
        self.tx += (right[0] * dx + fwd[0] * dy) * k
        self.ty += (right[1] * dx + fwd[1] * dy) * k
        self.apply()

    def zoom(self, factor: float) -> None:
        self.dist = min(DIST_MAX, max(DIST_MIN, self.dist * factor))
        self.apply()

    def focus(self, i: int) -> None:
        self.tx, self.ty = self.objects[i]
        self.dist = min(self.dist, 120.0)
        self.apply()

    def reset(self) -> None:
        (self.tx, self.ty), self.yaw, self.pitch, self.dist = self.home
        self.apply()

    def apply(self) -> None:
        p, y = math.radians(self.pitch), math.radians(self.yaw)
        h = self.dist * math.cos(p)
        cam = self.base.camera
        cam.setPos(self.tx - h * math.sin(y), self.ty - h * math.cos(y), self.dist * math.sin(p))
        cam.lookAt(Point3(self.tx, self.ty, 0))  # вертикаль мира остаётся вертикалью кадра: крена нет
        cam.setR(0)

    # ------------------------------------------------------------ мышь
    def _press(self, mode: str) -> None:
        self.mode = mode
        self.last = self._mouse()

    def _release(self) -> None:
        self.mode, self.last = None, None

    def _mouse(self) -> tuple[float, float] | None:
        mw = self.base.mouseWatcherNode
        if mw is None or not mw.hasMouse():
            return None
        m = mw.getMouse()
        return m.getX(), m.getY()

    def _task(self, task):
        if self.mode:
            cur = self._mouse()
            if cur and self.last:
                dx, dy = cur[0] - self.last[0], cur[1] - self.last[1]
                if self.mode == "rotate":
                    self.rotate(dx * 150.0, -dy * 70.0)
                else:
                    self.pan(-dx * 0.9, -dy * 0.9)
            self.last = cur
        return task.cont
