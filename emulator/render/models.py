"""Процедурные модели машин и людей с гладкими нормалями.

Модели смотрят вдоль +Y, начало координат — центр на земле, размеры в метрах.
Цель — узнаваемый силуэт для детектора: капот, лобовое стекло, крыша, колёса, фары, номера;
у человека — голова, шея, плечи, талия, руки и ноги с суставами.
"""
from __future__ import annotations

import math
import random

import numpy as np
from panda3d.core import (Geom, GeomNode, GeomTriangles, GeomVertexData, GeomVertexFormat, GeomVertexWriter,
                          Material, NodePath, PNMImage, SamplerState, Texture, TransparencyAttrib)


class SMesh:
    """Накопитель вершин (позиция, нормаль, цвет) и треугольников."""

    def __init__(self, name: str):
        self.name = name
        self.P, self.N, self.C, self.T = [], [], [], []

    def _v(self, p, n, c) -> int:
        self.P.append(p)
        self.N.append(n)
        self.C.append(c)
        return len(self.P) - 1

    # -------------------------------------------------------------- примитивы
    def grid(self, pts: np.ndarray, cols: np.ndarray, closed: bool = True, cap0: bool = True, cap1: bool = True):
        """Поверхность из колец: pts (S, M, 3), cols (S, M, 3). Нормали сглаженные, наружу."""
        S, M = pts.shape[:2]
        acc = np.zeros_like(pts)
        mm = M if closed else M - 1
        for i in range(S - 1):
            for j in range(mm):
                j2 = (j + 1) % M
                a, b, c, d = pts[i, j], pts[i, j2], pts[i + 1, j2], pts[i + 1, j]
                n = np.cross(c - a, d - b)
                for (ii, jj) in ((i, j), (i, j2), (i + 1, j2), (i + 1, j)):
                    acc[ii, jj] += n
        centers = pts.mean(axis=1, keepdims=True)
        out = pts - centers
        flip = np.sign(np.sum(acc * out, axis=2, keepdims=True))
        flip[flip == 0] = 1
        nrm = acc * flip
        nrm /= np.maximum(np.linalg.norm(nrm, axis=2, keepdims=True), 1e-9)
        base = len(self.P)
        for i in range(S):
            for j in range(M):
                self._v(tuple(pts[i, j]), tuple(nrm[i, j]), tuple(cols[i, j]))
        for i in range(S - 1):
            for j in range(mm):
                j2 = (j + 1) % M
                a, b, c, d = base + i * M + j, base + i * M + j2, base + (i + 1) * M + j2, base + (i + 1) * M + j
                self.T += [(a, b, c), (a, c, d)]
        for idx, cap in ((0, cap0), (S - 1, cap1)):
            if not cap:
                continue
            ring = pts[idx]
            ctr = ring.mean(axis=0)
            other = pts[1 if idx == 0 else S - 2].mean(axis=0)
            n = ctr - other
            n = n / max(np.linalg.norm(n), 1e-9)
            c0 = self._v(tuple(ctr), tuple(n), tuple(cols[idx].mean(axis=0)))
            ids = [self._v(tuple(ring[j]), tuple(n), tuple(cols[idx, j])) for j in range(M)]
            for j in range(M):
                self.T.append((c0, ids[j], ids[(j + 1) % M]))

    def ellipsoid(self, c, r, color, seg: int = 14, rings: int = 9, zcut: float = -1.0):
        """Эллипсоид с центром c и полуосями r; zcut > -1 срезает низ (для причёски)."""
        us = np.linspace(-math.pi / 2 * (1 if zcut <= -1 else 0) + (math.asin(zcut) if zcut > -1 else 0),
                         math.pi / 2, rings)
        pts, cols = [], []
        for u in us:
            ring = []
            for k in range(seg):
                v = 2 * math.pi * k / seg
                ring.append((c[0] + r[0] * math.cos(u) * math.cos(v), c[1] + r[1] * math.cos(u) * math.sin(v),
                             c[2] + r[2] * math.sin(u)))
            pts.append(ring)
            cols.append([color] * seg)
        self.grid(np.array(pts), np.array(cols, float), cap0=zcut > -1, cap1=False)

    def frustum(self, p0, p1, r0, r1, color, seg: int = 12, sx: float = 1.0):
        """Усечённый конус от p0 до p1 (радиусы r0, r1); sx — сплющивание по поперечной оси."""
        p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
        ax = p1 - p0
        L = np.linalg.norm(ax)
        ax /= L
        ref = np.array([1.0, 0, 0]) if abs(ax[0]) < 0.9 else np.array([0, 1.0, 0])
        u = np.cross(ax, ref)
        u /= np.linalg.norm(u)
        w = np.cross(ax, u)
        rings = []
        for t, r in ((0.0, r0), (0.5, (r0 + r1) / 2), (1.0, r1)):
            c = p0 + ax * L * t
            rings.append([c + (u * math.cos(2 * math.pi * k / seg) * r * sx + w * math.sin(2 * math.pi * k / seg) * r)
                          for k in range(seg)])
        self.grid(np.array(rings), np.full((3, seg, 3), color, float))

    def box(self, c, s, color):
        x0, x1 = c[0] - s[0] / 2, c[0] + s[0] / 2
        y0, y1 = c[1] - s[1] / 2, c[1] + s[1] / 2
        z0, z1 = c[2] - s[2] / 2, c[2] + s[2] / 2
        faces = [((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1)),
                 ((x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0), (0, 0, -1)),
                 ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0)),
                 ((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (0, 1, 0)),
                 ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1), (1, 0, 0)),
                 ((x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (-1, 0, 0))]
        for a, b, cc, d, n in faces:
            i = [self._v(p, n, color) for p in (a, b, cc, d)]
            self.T += [(i[0], i[1], i[2]), (i[0], i[2], i[3])]

    def wheel(self, x, y, z, r, width, tire=(0.06, 0.06, 0.07), rim=(0.62, 0.64, 0.67), side=1):
        """Колесо с осью вдоль X: шина и диск с внешней стороны."""
        self.frustum((x - width / 2, y, z), (x + width / 2, y, z), r, r, tire, seg=18)
        xo = x + side * (width / 2 + 0.005)
        self.frustum((xo - side * 0.01, y, z), (xo, y, z), r * 0.62, r * 0.62, rim, seg=14)
        self.frustum((xo, y, z), (xo + side * 0.012, y, z), r * 0.2, r * 0.2, (0.3, 0.3, 0.32), seg=8)

    def node(self) -> NodePath:
        vd = GeomVertexData(self.name, GeomVertexFormat.getV3n3c4(), Geom.UHStatic)
        vd.setNumRows(len(self.P))
        vw, nw, cw = (GeomVertexWriter(vd, n) for n in ("vertex", "normal", "color"))
        for p, n, c in zip(self.P, self.N, self.C):
            vw.addData3(*p)
            nw.addData3(*n)
            cw.addData4(c[0], c[1], c[2], 1.0)
        prim = GeomTriangles(Geom.UHStatic)
        for a, b, c in self.T:
            prim.addVertices(a, b, c)
        g = Geom(vd)
        g.addPrimitive(prim)
        gn = GeomNode(self.name)
        gn.addGeom(g)
        np_ = NodePath(gn)
        np_.setTwoSided(True)
        return np_


def _interp(y, keys):
    ys, vs = zip(*keys)
    return float(np.interp(y, ys, vs))


def superellipse_ring(cx, cz, a, b, M=20, n=4.0, taper_top=1.0):
    pts = []
    for k in range(M):
        t = 2 * math.pi * k / M
        c, s = math.cos(t), math.sin(t)
        x = a * math.copysign(abs(c) ** (2 / n), c)
        z = b * math.copysign(abs(s) ** (2 / n), s)
        if taper_top != 1.0 and z > 0:
            x *= 1 - (1 - taper_top) * (z / b)
        pts.append((cx + x, cz + z))
    return pts


# ============================================================================ машины
GLASS = (0.07, 0.09, 0.12)
DARK = (0.08, 0.08, 0.09)
PLATE = (0.93, 0.93, 0.9)
HEAD = (0.93, 0.93, 0.88)
TAIL = (0.62, 0.05, 0.04)

CAR_STYLES = {
    #          длина ширина высота  низ   пояс перед/зад  крыша  колесо  стекло: (низ лоб., верх лоб., верх зад., низ зад.)
    "sedan": dict(L=4.6, W=1.8, H=1.45, z0=0.3, belt=(0.78, 0.95), roof=1.45, wr=0.32,
                  cab=(0.55, -0.05, -1.05, -1.55)),
    "hatch": dict(L=4.1, W=1.76, H=1.5, z0=0.3, belt=(0.8, 0.97), roof=1.5, wr=0.31,
                  cab=(0.45, -0.15, -1.55, -1.95)),
    "suv": dict(L=4.6, W=1.88, H=1.72, z0=0.42, belt=(0.98, 1.1), roof=1.72, wr=0.37,
                cab=(0.75, 0.2, -1.6, -2.1)),
}


def car_body(style: str, paint=(1, 1, 1)) -> tuple[SMesh, SMesh]:
    """Кузов легковой машины: (окрашиваемая часть, детали). Окраска задаётся colorScale на узле кузова."""
    s = CAR_STYLES[style]
    L, W = s["L"], s["W"]
    hy = L / 2
    body, det = SMesh("body"), SMesh("details")
    # нижняя часть кузова: скруглённый брус, сужение к носу и корме, профиль капота и багажника
    ys = np.linspace(-hy, hy, 26)
    rings, cols = [], []
    for y in ys:
        f = abs(y) / hy
        w = W / 2 * (1 - 0.18 * f ** 8)
        top = _interp(y, [(-hy, s["belt"][1] - 0.12), (-hy + 0.35, s["belt"][1]), (hy - 1.3, s["belt"][0] + 0.06),
                          (hy - 0.3, s["belt"][0]), (hy, s["belt"][0] - 0.16)])
        bot = s["z0"] + (0.06 if f > 0.9 else 0.0)
        ring = superellipse_ring(0, (top + bot) / 2, w, (top - bot) / 2, M=22, n=5)
        rings.append([(x, y, z) for x, z in ring])
        cols.append([paint] * len(ring))
    body.grid(np.array(rings), np.array(cols, float))
    # салон: стёкла тёмные, крыша в цвет кузова
    wf0, wf1, wr1, wr0 = s["cab"]
    ys = np.linspace(wr0, wf0, 16)
    rings, cols = [], []
    for y in ys:
        belt = _interp(y, [(-hy, s["belt"][1]), (hy, s["belt"][0])])
        top = _interp(y, [(wr0, belt + 0.02), (wr1, s["roof"]), (wf1, s["roof"]), (wf0, belt + 0.02)])
        top = max(top, belt + 0.03)
        w = W / 2 * 0.93
        ring = superellipse_ring(0, (top + belt - 0.04) / 2, w, (top - belt + 0.04) / 2, M=22, n=4, taper_top=0.82)
        rings.append([(x, y, z) for x, z in ring])
        cols.append([paint if z > top - 0.05 or z < belt else GLASS for x, z in ring])
    body.grid(np.array(rings), np.array(cols, float))
    # колёса
    wr = s["wr"]
    for sx in (-1, 1):
        for y in (hy - 0.85 - (0.05 if style == "suv" else 0), -hy + 0.85):
            det.wheel(sx * (W / 2 - 0.1), y, wr, wr, 0.22, side=sx)
    fz = s["belt"][0] - 0.12
    rz = s["belt"][1] - 0.1
    for sx in (-1, 1):
        det.ellipsoid((sx * (W / 2 - 0.22), hy - 0.06, fz), (0.17, 0.06, 0.06), HEAD, seg=10, rings=6)   # фары
        det.box((sx * (W / 2 - 0.2), -hy + 0.03, rz), (0.3, 0.06, 0.1), TAIL)                            # фонари
        det.box((sx * (W / 2 + 0.06), wf0 - 0.1, s["belt"][0] + 0.12), (0.12, 0.08, 0.1), paint)           # зеркала
    det.box((0, hy - 0.02, fz - 0.05), (0.7, 0.06, 0.12), DARK)                                          # решётка
    det.box((0, hy + 0.01, s["z0"] + 0.12), (0.52, 0.03, 0.12), PLATE)                                   # номер спереди
    det.box((0, -hy - 0.01, s["z0"] + 0.25), (0.52, 0.03, 0.12), PLATE)                                  # номер сзади
    det.box((0, hy - 0.05, s["z0"] + 0.02), (W * 0.92, 0.12, 0.1), DARK)                                 # бамперы
    det.box((0, -hy + 0.05, s["z0"] + 0.02), (W * 0.92, 0.12, 0.1), DARK)
    return body, det


def bus_mesh(paint) -> tuple[SMesh, SMesh]:
    L, W, H = 12.0, 2.55, 3.0
    hy = L / 2
    body, det = SMesh("body"), SMesh("details")
    ys = np.linspace(-hy, hy, 20)
    rings, cols = [], []
    for y in ys:
        f = abs(y) / hy
        w = W / 2 * (1 - 0.05 * f ** 12)
        top = H - (0.15 * f ** 10)
        ring = superellipse_ring(0, (top + 0.35) / 2, w, (top - 0.35) / 2, M=24, n=7)
        rings.append([(x, y, z) for x, z in ring])
        front = y > hy - 0.25
        cols.append([GLASS if (1.15 < z < 2.6 and (abs(x) > w * 0.8 or front) and not (front and z < 1.1))
                     else paint for x, z in ring])
    body.grid(np.array(rings), np.array(cols, float))
    for sx in (-1, 1):
        for y in (hy - 2.6, -hy + 3.0):
            det.wheel(sx * (W / 2 - 0.2), y, 0.5, 0.5, 0.3, side=sx)
    for y in (hy - 1.2, 0.3, -hy + 2.0):  # двери справа
        det.box((W / 2 + 0.01, y, 1.45), (0.03, 1.2, 2.2), (0.12, 0.14, 0.16))
    det.box((0, hy + 0.01, 2.75), (1.6, 0.04, 0.25), (0.95, 0.55, 0.1))   # табло маршрута
    for sx in (-1, 1):
        det.box((sx * 0.9, hy + 0.01, 0.7), (0.3, 0.04, 0.15), HEAD)
    det.box((0, hy + 0.02, 0.45), (0.52, 0.03, 0.12), PLATE)
    return body, det


def truck_mesh(paint) -> tuple[SMesh, SMesh]:
    L, W = 7.0, 2.4
    hy = L / 2
    body, det = SMesh("body"), SMesh("details")
    # кабина
    ys = np.linspace(hy - 2.2, hy, 10)
    rings, cols = [], []
    for y in ys:
        top = _interp(y, [(hy - 2.2, 2.7), (hy - 0.5, 2.7), (hy, 2.2)])
        ring = superellipse_ring(0, (top + 0.5) / 2, W / 2 * 0.98, (top - 0.5) / 2, M=22, n=6)
        rings.append([(x, y, z) for x, z in ring])
        cols.append([GLASS if (z > 1.65 and (y > hy - 0.6 or abs(x) > W / 2 * 0.85) and y > hy - 1.3) else (0.85, 0.86, 0.88)
                     for x, z in ring])
    det.grid(np.array(rings), np.array(cols, float))
    body.box((0, -1.25, 1.95), (W + 0.05, L - 2.5, 2.3), paint)       # фургон
    det.box((0, -1.25, 0.65), (W * 0.6, L - 2.5, 0.3), DARK)         # рама
    for sx in (-1, 1):
        for y in (hy - 1.2, -hy + 1.3, -hy + 2.4):
            det.wheel(sx * (W / 2 - 0.2), y, 0.5, 0.5, 0.3, side=sx)
        det.box((sx * 0.8, hy + 0.01, 0.85), (0.3, 0.04, 0.15), HEAD)
    det.box((0, hy + 0.02, 0.55), (0.52, 0.03, 0.12), PLATE)
    return body, det


def ambulance_mesh() -> tuple[SMesh, SMesh, SMesh]:
    L, W, H = 5.6, 2.1, 2.6
    hy = L / 2
    body, det, bar = SMesh("body"), SMesh("details"), SMesh("lightbar")
    ys = np.linspace(-hy, hy, 22)
    rings, cols = [], []
    for y in ys:
        top = _interp(y, [(-hy, H), (hy - 1.4, H), (hy - 0.5, 1.45), (hy, 1.05)])
        ring = superellipse_ring(0, (top + 0.35) / 2, W / 2, (top - 0.35) / 2, M=24, n=6)
        rings.append([(x, y, z) for x, z in ring])
        row = []
        for x, z in ring:
            if 1.05 < z < 1.3 and y < hy - 0.9:
                row.append((0.85, 0.08, 0.08))  # красная полоса
            elif z > 1.3 and y > hy - 1.6 and (y > hy - 1.0 or abs(x) > W / 2 * 0.85):
                row.append(GLASS)
            else:
                row.append((0.95, 0.95, 0.94))
        cols.append(row)
    body.grid(np.array(rings), np.array(cols, float))
    for sx in (-1, 1):
        for y in (hy - 0.9, -hy + 1.0):
            det.wheel(sx * (W / 2 - 0.15), y, 0.36, 0.36, 0.24, side=sx)
        det.box((sx * 0.75, hy - 0.05, 0.85), (0.3, 0.06, 0.12), HEAD)
    det.box((0, hy + 0.01, 0.5), (0.52, 0.03, 0.12), PLATE)
    bar.box((0, hy - 1.7, H + 0.1), (1.2, 0.3, 0.2), (1, 1, 1))
    return body, det, bar


def build_vehicle(kind: str, style: str | None, paint) -> tuple[NodePath, NodePath, NodePath | None]:
    """(корень, кузов для окраски, мигалка)."""
    root = NodePath(f"veh_{kind}")
    bar = None
    if kind == "car":
        body, det = car_body(style or "sedan")
        paint_node = True
    elif kind == "bus":
        body, det = bus_mesh((1, 1, 1))
        paint_node = True
    elif kind == "truck":
        body, det = truck_mesh((1, 1, 1))
        paint_node = True
    else:
        body, det, barm = ambulance_mesh()
        bar = barm.node()
        bar.reparentTo(root)
        bar.setLightOff()
        bar.setName("lightbar")
        paint_node = False
    b = body.node()
    b.setName("body")
    b.reparentTo(root)
    d = det.node()
    d.reparentTo(root)
    mat = Material()
    mat.setSpecular((0.55, 0.55, 0.55, 1))
    mat.setShininess(45)
    root.setMaterial(mat)  # один материал на всю машину: кузов и детали склеиваются в одну геометрию
    if not paint_node:
        b.setColorScale(1, 1, 1, 1)
    return root, b, bar


# ============================================================================ люди
SKIN = [(0.93, 0.78, 0.66), (0.86, 0.68, 0.55), (0.75, 0.57, 0.44), (0.55, 0.39, 0.29), (0.95, 0.83, 0.72)]
HAIR = [(0.08, 0.06, 0.05), (0.25, 0.16, 0.08), (0.45, 0.32, 0.18), (0.7, 0.6, 0.4), (0.55, 0.55, 0.55)]
TOPS = [(0.12, 0.14, 0.2), (0.45, 0.1, 0.1), (0.2, 0.3, 0.22), (0.62, 0.56, 0.45), (0.1, 0.1, 0.1), (0.5, 0.5, 0.55),
        (0.75, 0.35, 0.2), (0.25, 0.35, 0.6), (0.85, 0.85, 0.82), (0.45, 0.25, 0.45), (0.3, 0.45, 0.55)]
PANTS = [(0.1, 0.12, 0.18), (0.08, 0.08, 0.08), (0.3, 0.3, 0.32), (0.22, 0.26, 0.38), (0.4, 0.34, 0.26)]
SHOES = [(0.05, 0.05, 0.05), (0.85, 0.85, 0.85), (0.25, 0.15, 0.1)]


def build_person(rng: random.Random) -> tuple[NodePath, list[NodePath]]:
    """Человек ростом 1,75 м. Возвращает (корень, [бедро Л, бедро П, колено Л, колено П, плечо Л, плечо П, локоть Л, локоть П])."""
    skin, hair = rng.choice(SKIN), rng.choice(HAIR)
    top, pants, shoes = rng.choice(TOPS), rng.choice(PANTS), rng.choice(SHOES)
    long_hair = rng.random() < 0.35
    root = NodePath("person")
    core = SMesh("core")
    # таз и туловище
    core.ellipsoid((0, 0, 0.95), (0.165, 0.105, 0.1), pants, seg=14, rings=7)
    zs = [0.92, 1.0, 1.12, 1.25, 1.38, 1.44, 1.47]
    rx = [0.15, 0.145, 0.16, 0.18, 0.2, 0.17, 0.08]
    ry = [0.1, 0.095, 0.105, 0.115, 0.11, 0.09, 0.06]
    rings = []
    for z, a, b in zip(zs, rx, ry):
        rings.append([(a * math.cos(2 * math.pi * k / 16), b * math.sin(2 * math.pi * k / 16), z) for k in range(16)])
    core.grid(np.array(rings), np.full((len(zs), 16, 3), top, float))
    core.frustum((0, 0, 1.44), (0, 0.005, 1.55), 0.05, 0.045, skin, seg=10)                  # шея
    core.ellipsoid((0, 0.005, 1.64), (0.083, 0.098, 0.115), skin, seg=14, rings=9)           # голова
    core.ellipsoid((0, -0.012, 1.665), (0.09, 0.105, 0.105), hair, seg=14, rings=7, zcut=-0.05)  # волосы
    if long_hair:
        core.ellipsoid((0, -0.06, 1.52), (0.085, 0.05, 0.13), hair, seg=10, rings=6)
    core.ellipsoid((0.0, 0.1, 1.64), (0.018, 0.02, 0.025), skin, seg=6, rings=4)             # нос
    if rng.random() < 0.3:  # рюкзак
        core.box((0, -0.17, 1.2), (0.28, 0.14, 0.36), rng.choice(TOPS))
    c = core.node()
    c.setName("core")
    c.reparentTo(root)
    joints = []
    # ноги: бедро → колено → голень и стопа
    for sx in (-1, 1):
        hip = root.attachNewNode(f"hip{sx}")
        hip.setPos(sx * 0.09, 0, 0.92)
        m = SMesh("thigh")
        m.frustum((0, 0, 0), (0, 0, -0.43), 0.075, 0.055, pants, seg=12)
        m.node().reparentTo(hip)
        knee = hip.attachNewNode("knee")
        knee.setPos(0, 0, -0.43)
        m = SMesh("shin")
        m.frustum((0, 0, 0), (0, 0, -0.41), 0.055, 0.042, pants, seg=12)
        m.ellipsoid((0, 0.05, -0.44), (0.055, 0.13, 0.045), shoes, seg=10, rings=6)
        m.node().reparentTo(knee)
        joints.append((hip, knee))
    arms = []
    for sx in (-1, 1):
        sh = root.attachNewNode(f"shoulder{sx}")
        sh.setPos(sx * 0.205, 0, 1.4)
        m = SMesh("upper")
        m.ellipsoid((0, 0, 0), (0.055, 0.055, 0.05), top, seg=10, rings=6)
        m.frustum((0, 0, 0), (0, 0, -0.29), 0.048, 0.04, top, seg=10)
        m.node().reparentTo(sh)
        el = sh.attachNewNode("elbow")
        el.setPos(0, 0, -0.29)
        m = SMesh("fore")
        m.frustum((0, 0, 0), (0, 0, -0.25), 0.04, 0.033, top, seg=10)
        m.ellipsoid((0, 0.005, -0.3), (0.035, 0.028, 0.055), skin, seg=8, rings=5)
        m.node().reparentTo(el)
        arms.append((sh, el))
    out = [joints[0][0], joints[1][0], joints[0][1], joints[1][1], arms[0][0], arms[1][0], arms[0][1], arms[1][1]]
    return root, out


def animate_person(joints: list[NodePath], phase: float, moving: bool) -> None:
    hl, hr, kl, kr, sl, sr, el, er = joints
    if not moving:
        for j in joints:
            j.setP(0)
        el.setP(8)
        er.setP(8)
        return
    s = math.sin(phase)
    hl.setP(26 * s)
    hr.setP(-26 * s)
    # колено сгибается, когда нога идёт назад и выносится вперёд
    kl.setP(-max(0.0, math.sin(phase + 1.2)) * 45)
    kr.setP(-max(0.0, math.sin(phase + 1.2 + math.pi)) * 45)
    sl.setP(-20 * s)
    sr.setP(20 * s)
    el.setP(18)
    er.setP(18)


def contact_shadow_texture() -> Texture:
    img = PNMImage(64, 64, 4)
    for y in range(64):
        for x in range(64):
            d = math.hypot((x - 31.5) / 32, (y - 31.5) / 32)
            a = max(0.0, 1 - d) ** 1.6 * 0.55
            img.setXelA(x, y, 0, 0, 0, a)
    tex = Texture("shadow")
    tex.load(img)
    tex.setMinfilter(SamplerState.FT_linear)
    tex.setMagfilter(SamplerState.FT_linear)
    return tex


def contact_shadow(parent: NodePath, tex: Texture, sx: float, sy: float) -> NodePath:
    from panda3d.core import CardMaker
    cm = CardMaker("shadow")
    cm.setFrame(-sx / 2, sx / 2, -sy / 2, sy / 2)
    np_ = parent.attachNewNode(cm.generate())
    np_.setP(-90)
    np_.setZ(0.02)
    np_.setTexture(tex)
    np_.setTransparency(TransparencyAttrib.MAlpha)
    np_.setDepthWrite(False)
    np_.setLightOff()
    np_.setBin("transparent", 0)
    np_.setShaderOff()
    return np_
