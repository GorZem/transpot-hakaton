"""3D-сцена участка на Panda3D: статичная геометрия из дорожной сети и домов OSM, машины, пешеходы, светофоры."""
from __future__ import annotations

import math
import random

import numpy as np
from panda3d.core import (AmbientLight, DirectionalLight, Geom, GeomNode, GeomTriangles, GeomVertexData,
                          GeomVertexFormat, GeomVertexWriter, NodePath, Triangulator, Vec3, Vec4)

from emulator.world.geom import Polyline, right_normal, unit
from emulator.world.network import CROSSWALK_W, LANE_W, SIDEWALK_W, Network
from emulator.world.sim import World

ASPHALT = (0.25, 0.26, 0.28)
ASPHALT_JUNCTION = (0.27, 0.28, 0.3)
SIDEWALK = (0.6, 0.6, 0.58)
CURB = (0.7, 0.7, 0.68)
GRASS = (0.33, 0.42, 0.27)
WHITE = (0.92, 0.92, 0.9)
YELLOW_PAINT = (0.95, 0.78, 0.2)
POLE = (0.35, 0.37, 0.4)
HOUSING = (0.1, 0.11, 0.12)
BUILDING_COLORS = [(0.82, 0.8, 0.75), (0.75, 0.72, 0.66), (0.86, 0.84, 0.8), (0.7, 0.66, 0.6),
                   (0.78, 0.74, 0.7), (0.8, 0.76, 0.66), (0.66, 0.68, 0.7), (0.84, 0.78, 0.7)]

Z_ASPHALT, Z_JUNCTION, Z_MARK, Z_SIDEWALK = 0.03, 0.035, 0.06, 0.15


class Mesh:
    """Накопитель треугольников с цветами и нормалями в один Geom."""

    def __init__(self, name: str):
        self.name = name
        self.vdata = GeomVertexData(name, GeomVertexFormat.getV3n3c4(), Geom.UHStatic)
        self.vw = GeomVertexWriter(self.vdata, "vertex")
        self.nw = GeomVertexWriter(self.vdata, "normal")
        self.cw = GeomVertexWriter(self.vdata, "color")
        self.prim = GeomTriangles(Geom.UHStatic)
        self.n = 0

    def tri(self, a, b, c, color, normal=None):
        a, b, c = (Vec3(*p) for p in (a, b, c))
        if normal is None:
            nrm = (b - a).cross(c - a)
            if nrm.length() < 1e-9:
                return
            nrm.normalize()
        else:
            nrm = Vec3(*normal)
        for p in (a, b, c):
            self.vw.addData3(p)
            self.nw.addData3(nrm)
            self.cw.addData4(*color, 1.0)
        self.prim.addVertices(self.n, self.n + 1, self.n + 2)
        self.n += 3

    def quad(self, a, b, c, d, color, normal=None):
        self.tri(a, b, c, color, normal)
        self.tri(a, c, d, color, normal)

    def box(self, cx, cy, cz, sx, sy, sz, color, bottom=True):
        """Параллелепипед по центру основания (cx, cy, cz), размеры sx, sy, sz."""
        x0, x1, y0, y1, z0, z1 = cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2, cz, cz + sz
        self.quad((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), color, (0, 0, 1))
        if bottom:
            self.quad((x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0), color, (0, 0, -1))
        self.quad((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), color, (0, -1, 0))
        self.quad((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1), color, (0, 1, 0))
        self.quad((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1), color, (1, 0, 0))
        self.quad((x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1), color, (-1, 0, 0))

    def cylinder(self, cx, cy, z0, r, h, color, seg=10, axis="z"):
        for i in range(seg):
            a0, a1 = 2 * math.pi * i / seg, 2 * math.pi * (i + 1) / seg
            c0, s0, c1, s1 = math.cos(a0), math.sin(a0), math.cos(a1), math.sin(a1)
            if axis == "z":
                p = lambda c, s, z: (cx + r * c, cy + r * s, z)
                self.quad(p(c0, s0, z0), p(c1, s1, z0), p(c1, s1, z0 + h), p(c0, s0, z0 + h), color,
                          ((c0 + c1) / 2, (s0 + s1) / 2, 0))
                self.tri(p(0, 0, z0 + h), p(c0, s0, z0 + h), p(c1, s1, z0 + h), color, (0, 0, 1))
            else:  # ось x: колесо, cx — центр по x, cy — y, z0 — высота центра
                p = lambda c, s, x: (x, cy + r * c, z0 + r * s)
                x0, x1 = cx - h / 2, cx + h / 2
                self.quad(p(c0, s0, x0), p(c1, s1, x0), p(c1, s1, x1), p(c0, s0, x1), color,
                          (0, (c0 + c1) / 2, (s0 + s1) / 2))
                self.tri(p(0, 0, x1), p(c0, s0, x1), p(c1, s1, x1), color, (1, 0, 0))
                self.tri(p(0, 0, x0), p(c1, s1, x0), p(c0, s0, x0), color, (-1, 0, 0))

    def sphere(self, cx, cy, cz, r, color, seg=10, rings=6):
        def pt(i, j):
            th, ph = 2 * math.pi * i / seg, math.pi * j / rings
            return (cx + r * math.sin(ph) * math.cos(th), cy + r * math.sin(ph) * math.sin(th), cz + r * math.cos(ph))
        for j in range(rings):
            for i in range(seg):
                a, b, c, d = pt(i, j), pt(i + 1, j), pt(i + 1, j + 1), pt(i, j + 1)
                nrm = tuple((np.array(a) + np.array(c)) / 2 - np.array((cx, cy, cz)))
                self.quad(a, d, c, b, color, nrm)

    def ribbon(self, poly: Polyline, d0: float, d1: float, z: float, color):
        """Полоса между сдвигами d0 и d1 (вправо положительные) вдоль ломаной."""
        L, R = poly.offset(d0).p, poly.offset(d1).p
        n = min(len(L), len(R))
        for i in range(n - 1):
            self.quad((*L[i], z), (*R[i], z), (*R[i + 1], z), (*L[i + 1], z), color, (0, 0, 1))

    def node(self) -> NodePath:
        g = Geom(self.vdata)
        g.addPrimitive(self.prim)
        gn = GeomNode(self.name)
        gn.addGeom(g)
        return NodePath(gn)


# ---------------------------------------------------------------------------------------------- статика
def draw_arrow(mesh: "Mesh", poly: Polyline, s0: float, turns: set) -> None:
    """Стрелка 1.18 на полосе: стержень и наконечники для каждого разрешённого направления."""
    p = poly.point(s0)
    d = unit(poly.direction(s0))
    r = right_normal(d)
    z = Z_MARK + 0.004

    def pt(f, lat):  # стрелка около 5 м длиной, как на настоящей разметке
        q = p + d * f * 1.65 + r * lat * 1.4
        return (q[0], q[1], z)

    def quad(a, b, c, e):
        mesh.quad(a, b, c, e, WHITE, (0, 0, 1))

    quad(pt(-2.6, -0.08), pt(-2.6, 0.08), pt(0.2, 0.08), pt(0.2, -0.08))  # стержень
    if "straight" in turns:
        quad(pt(0.2, -0.08), pt(0.2, 0.08), pt(0.9, 0.08), pt(0.9, -0.08))
        mesh.tri(pt(0.9, -0.3), pt(0.9, 0.3), pt(1.7, 0.0), WHITE, (0, 0, 1))
    for side, t in ((1, "right"), (-1, "left")):
        if t in turns:
            quad(pt(-0.5, -0.08 * side), pt(-0.2, 0.08 * side), pt(0.35, 0.75 * side), pt(0.05, 0.62 * side))
            mesh.tri(pt(-0.05, 0.5 * side), pt(0.55, 1.0 * side), pt(0.55, 0.3 * side), WHITE, (0, 0, 1))


def build_static(root: NodePath, net: Network, rng: random.Random, vis=None) -> None:
    """vis — зона видимости камер: вне её дома без окон, без деревьев, бордюров и прерывистой разметки."""
    near = (lambda x, y: True) if vis is None else vis.detailed
    ground, road, marks, walks, bld, trees = (Mesh(n) for n in ("ground", "road", "marks", "walks", "bld", "trees"))
    x0, y0, x1, y1 = net.bounds
    m = 400
    ground.quad((x0 - m, y0 - m, 0), (x1 + m, y0 - m, 0), (x1 + m, y1 + m, 0), (x0 - m, y1 + m, 0), GRASS, (0, 0, 1))

    for e in net.edges:
        poly = e.poly
        road.ribbon(poly, -e.half_left, e.half_right, Z_ASPHALT, ASPHALT)
        # тротуары, обрезанные у перекрёстков
        ta = e.a.radius if e.a.kind in ("junction", "priority") else 0.0
        tb = e.b.radius if e.b.kind in ("junction", "priority") else 0.0
        if poly.length - ta - tb > 1:
            sp = poly.sub(ta, poly.length - tb)
            walks.ribbon(sp, e.half_right, e.half_right + SIDEWALK_W, Z_SIDEWALK, SIDEWALK)
            walks.ribbon(sp, -e.half_left - SIDEWALK_W, -e.half_left, Z_SIDEWALK, SIDEWALK)
            for d in (e.half_right, -e.half_left):  # бордюр
                if not any(near(*q) for q in sp.p):
                    continue
                Lp = sp.offset(d).p
                for i in range(len(Lp) - 1):
                    walks.quad((*Lp[i], 0.0), (*Lp[i + 1], 0.0), (*Lp[i + 1], Z_SIDEWALK), (*Lp[i], Z_SIDEWALK), CURB)
            # деревья вдоль улицы, но не у светофорных объектов (не закрывать камеры)
            s = rng.uniform(4, 10)
            while s < sp.length:
                p = sp.point(s)
                if near(p[0], p[1]) and min((float(np.hypot(*(p - n.xy))) for n in net.signal_nodes), default=1e9) > 30:
                    for side, d in ((1, e.half_right + SIDEWALK_W + 1.8), (-1, -e.half_left - SIDEWALK_W - 1.8)):
                        if rng.random() < 0.7:
                            q = sp.offset(d).point(s)
                            h = rng.uniform(4, 7)
                            trees.cylinder(q[0], q[1], 0, 0.18, h * 0.45, (0.3, 0.22, 0.15), seg=6)
                            trees.sphere(q[0], q[1], h * 0.62, rng.uniform(1.6, 2.4),
                                         (0.2 + rng.random() * 0.08, 0.36 + rng.random() * 0.1, 0.16), seg=8, rings=5)
                s += rng.uniform(9, 14)
        # осевая: двойная сплошная у двусторонних улиц
        if e.lanes_fwd and e.lanes_bwd:
            la = next(l for l in e.lanes if l.forward)
            t0 = max(0.0, e.poly.project(la.poly.point(0))[0])
            t1 = min(e.poly.length, e.poly.project(la.poly.point(la.length))[0])
            if t1 - t0 > 1:
                cp = poly.sub(t0, t1)
                marks.ribbon(cp, -0.2, -0.08, Z_MARK, WHITE)
                marks.ribbon(cp, 0.08, 0.2, Z_MARK, WHITE)
        # прерывистые между полосами одного направления
        for lane in e.lanes:
            if lane.index == 0:
                continue
            lp = lane.poly.offset(-LANE_W / 2)
            # перед перекрёстком — сплошная: перестраиваться нельзя (как в модели движения)
            solid_from = lp.length - 15.0 if lane.to_node.kind in ("junction", "priority") else lp.length
            s = 0.0
            while s < solid_from - 1:
                q = lp.point(s)
                if near(q[0], q[1]):
                    marks.ribbon(lp.sub(s, min(s + 3, solid_from)), -0.06, 0.06, Z_MARK, WHITE)
                s += 9
            if solid_from < lp.length and near(*lp.point(lp.length)):
                marks.ribbon(lp.sub(max(0.0, solid_from), lp.length), -0.07, 0.07, Z_MARK, WHITE)
    for n in net.nodes:
        if n.kind in ("junction", "priority"):
            r = n.radius + CROSSWALK_W + 1.0
            seg = 24
            for i in range(seg):
                a0, a1 = 2 * math.pi * i / seg, 2 * math.pi * (i + 1) / seg
                road.tri((*n.xy, Z_JUNCTION), (n.xy[0] + r * math.cos(a0), n.xy[1] + r * math.sin(a0), Z_JUNCTION),
                         (n.xy[0] + r * math.cos(a1), n.xy[1] + r * math.sin(a1), Z_JUNCTION), ASPHALT_JUNCTION, (0, 0, 1))
    # стрелки направлений движения по полосам перед перекрёстками
    for lane in net.lanes:
        if lane.to_node.kind in ("junction", "priority") and lane.turns and lane.length > 22:
            p = lane.poly.point(lane.length - 12)
            if near(p[0], p[1]):
                draw_arrow(marks, lane.poly, lane.length - 12, lane.turns)
    # стоп-линии
    for lane in net.lanes:
        if lane.to_node.signal is not None or lane.to_node.kind == "crossing":
            p, d = lane.poly.point(lane.length), unit(lane.poly.direction(lane.length))
            nrm = right_normal(d)
            a, b = p - nrm * LANE_W / 2, p + nrm * LANE_W / 2
            c, dd = b - d * 0.4, a - d * 0.4
            marks.quad((*dd, Z_MARK), (*c, Z_MARK), (*b, Z_MARK), (*a, Z_MARK), WHITE, (0, 0, 1))
    # «зебры» 1.14.1: полосы 0,5 м через 0,5 м, бело-жёлтые
    for cw in net.crosswalks:
        k, i = -cw.length / 2 + 0.25, 0
        while k < cw.length / 2:
            c = cw.center + cw.along * k
            a = c - cw.along * 0.25 - cw.road_dir * CROSSWALK_W / 2
            b = c + cw.along * 0.25 - cw.road_dir * CROSSWALK_W / 2
            cc = b + cw.road_dir * CROSSWALK_W
            dd = a + cw.road_dir * CROSSWALK_W
            marks.quad((*a, Z_MARK + 0.005), (*b, Z_MARK + 0.005), (*cc, Z_MARK + 0.005), (*dd, Z_MARK + 0.005),
                       WHITE if i % 2 == 0 else YELLOW_PAINT, (0, 0, 1))
            k += 1.0
            i += 1
    # дома
    for b in net.buildings:
        pts, h = b["pts"], b["height"]
        col = BUILDING_COLORS[b["id"] % len(BUILDING_COLORS)]
        detail = vis is None or vis.building_detailed(pts)
        area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))
        if area < 0:
            pts = pts[::-1]
        for i in range(len(pts)):
            a, c = pts[i], pts[(i + 1) % len(pts)]
            bld.quad((*a, 0), (*c, 0), (*c, h), (*a, h), col)
            # полосы окон: тёмные пояса на каждом этаже
            if h >= 6 and detail:
                L = math.hypot(c[0] - a[0], c[1] - a[1])
                if L > 3:
                    dx, dy = (c[0] - a[0]) / L, (c[1] - a[1]) / L
                    nx, ny = dy * 0.03, -dx * 0.03
                    z = 1.1
                    while z + 1.4 < h:
                        p0 = (a[0] + dx * 0.8 + nx, a[1] + dy * 0.8 + ny)
                        p1 = (c[0] - dx * 0.8 + nx, c[1] - dy * 0.8 + ny)
                        bld.quad((*p0, z), (*p1, z), (*p1, z + 1.3), (*p0, z + 1.3), (0.28, 0.32, 0.38))
                        z += 3.0
        tr = Triangulator()
        for p in pts:
            tr.addPolygonVertex(tr.addVertex(*p))
        tr.triangulate()
        roof = tuple(c * 0.8 for c in col)
        for i in range(tr.getNumTriangles()):
            v = [tr.getVertex(j) for j in (tr.getTriangleV0(i), tr.getTriangleV1(i), tr.getTriangleV2(i))]
            bld.tri((v[0][0], v[0][1], h), (v[1][0], v[1][1], h), (v[2][0], v[2][1], h), roof, (0, 0, 1))
    for mesh in (ground, road, walks, marks, bld, trees):
        np_ = mesh.node()
        np_.reparentTo(root)
        np_.setTwoSided(True)
    root.flattenStrong()


# ---------------------------------------------------------------------------------------------- светофоры
class Lamp:
    __slots__ = ("np", "sc", "group", "color", "lit")

    def __init__(self, np_, sc, group, color):
        self.np, self.sc, self.group, self.color = np_, sc, group, color
        self.lit = None


LAMP_ON = {"r": (1.0, 0.12, 0.08), "y": (1.0, 0.75, 0.05), "g": (0.1, 1.0, 0.45)}
LAMP_OFF = {"r": (0.22, 0.05, 0.04), "y": (0.22, 0.17, 0.03), "g": (0.03, 0.18, 0.09)}


def lamp_on(kind: str, state: str, blink: bool) -> bool:
    return {
        "r": state in ("red", "red_yellow"),
        "y": state in ("yellow", "red_yellow") or (state == "flash_yellow" and blink),
        "g": state == "green" or (state == "green_blink" and blink),
    }[kind]


def build_signals(root: NodePath, net: Network, lamp_proto: NodePath) -> list[Lamp]:
    lamps: list[Lamp] = []
    hw = Mesh("signal_hw")

    def head(pos, facing, sc, group, kinds, z):
        """Секция светофора на опоре: pos — точка опоры, facing — куда смотрят сигналы."""
        f = unit(np.asarray(facing, float))
        hw.cylinder(pos[0], pos[1], 0, 0.07, z + 0.6, POLE, seg=6)
        n = len(kinds)
        hgt = 0.34 * n + 0.1
        c = np.asarray(pos) + f * 0.12
        hw.box(c[0], c[1], z - hgt / 2, 0.36, 0.36, hgt, HOUSING)
        for i, k in enumerate(kinds):
            lz = z + hgt / 2 - 0.22 - i * 0.34
            p = c + f * 0.19
            lnp = lamp_proto.copyTo(root)
            lnp.setPos(p[0], p[1], lz)
            lnp.setScale(0.13 if len(kinds) == 3 else 0.15)
            lamps.append(Lamp(lnp, sc, group, k))

    for n in net.nodes:
        sc = n.signal
        if sc is None:
            continue
        for arm in n.arms:
            lanes = arm.lanes_in()
            if lanes:
                right = max(lanes, key=lambda l: l.index)
                p = right.poly.point(right.length)
                d = unit(right.poly.direction(right.length))
                pole = p + right_normal(d) * (LANE_W / 2 + 0.8) + d * 1.0
                group = "veh" if sc.kind == "crossing" else f"veh_{arm.axis}"
                head(pole, -d, sc, group, "ryg", 3.4)
        seen = set()
        for arm in n.arms:
            cw = arm.crosswalk
            if cw is None or cw.id in seen:
                continue
            seen.add(cw.id)
            for side in (0, 1):
                out = cw.along if side == 1 else -cw.along
                pos = cw.end(side) + out * 0.7 + cw.road_dir * (CROSSWALK_W / 2 + 0.4)
                head(pos, -out, sc, cw.group, "rg", 2.6)
    hwn = hw.node()
    hwn.reparentTo(root)
    hwn.flattenStrong()
    return lamps


# ---------------------------------------------------------------------------------------------- агенты
class AgentView:
    """Синхронизирует узлы сцены с машинами и пешеходами модели.

    Модели агентов вне зоны видимости камер (и вне 3D-окна) убираются из сцены: агенты продолжают
    двигаться в модели, но камеры не тратят на них время. Модель создаётся при первом появлении в зоне.
    """

    POSES = 8

    def __init__(self, root: NodePath, world: World, vis=None, window_cam: NodePath | None = None, variants: int = 30):
        from panda3d.core import Point2, Point3
        from emulator.render import models
        self.models = models
        self.render = root
        self.root = root.attachNewNode("agents")
        self.world = world
        self.vis = vis
        self.window_cam = window_cam
        self._P2, self._P3 = Point2, Point3
        self.shadow_tex = models.contact_shadow_texture()
        self.veh_protos: dict[tuple, NodePath] = {}
        rng = random.Random(11)
        # позы шага заранее «запекаются» в цельные модели: при анимации переключается готовая поза,
        # и видеокарта рисует человека одной командой вместо десятка
        from panda3d.core import SwitchNode
        self.ped_protos = []
        for _ in range(variants):
            person, joints = models.build_person(rng)
            proto = NodePath("ped")
            sw = proto.attachNewNode(SwitchNode("poses"))
            for k in range(self.POSES + 1):
                models.animate_person(joints, 2 * math.pi * k / self.POSES, k < self.POSES)
                pose = person.copyTo(sw)
                pose.flattenStrong()
            models.contact_shadow(proto, self.shadow_tex, 0.8, 0.8)
            self.ped_protos.append(proto)
        self.cars: dict[int, list] = {}   # id -> [узел, мигалка, показан]
        self.peds: dict[int, list] = {}   # id -> [узел, суставы, показан]
        self.shown = {"cars": 0, "peds": 0}

    def _veh_proto(self, kind: str, style: str | None, length: float, width: float) -> NodePath:
        key = (kind, style)
        if key not in self.veh_protos:
            root, _body, _bar = self.models.build_vehicle(kind, style, (1, 1, 1))
            self.models.contact_shadow(root, self.shadow_tex, width + 0.9, length + 0.9)
            self.veh_protos[key] = root
        return self.veh_protos[key]

    def _visible(self, x: float, y: float) -> bool:
        if self.vis is None or self.vis.agent_visible(x, y):
            return True
        if self.window_cam is not None:
            p = self.window_cam.getRelativePoint(self.render, self._P3(x, y, 1.0))
            if 0 < p.y < 900 and self.window_cam.node().getLens().project(p, self._P2()):
                return True
        return False

    @staticmethod
    def _show(entry: list, on: bool) -> None:
        if entry[2] != on:
            entry[0].unstash() if on else entry[0].stash()
            entry[2] = on

    def sync(self, t: float) -> None:
        live, shown = set(), 0
        blink = (0.1, 0.3, 1.0) if int(t * 4) % 2 else (1.0, 0.1, 0.1)
        for c in self.world.cars:
            live.add(c.id)
            vis = self._visible(c.x, c.y)
            e = self.cars.get(c.id)
            if e is None:
                if not vis:
                    continue
                np_ = self._veh_proto(c.kind, c.style, c.length, c.width).copyTo(self.root)
                if c.kind != "emergency":
                    np_.find("body").setColorScale(*c.color, 1)
                    np_.flattenStrong()  # кузов и детали в одну модель с цветом этой машины
                bar = np_.find("lightbar")
                e = self.cars[c.id] = [np_, None if bar.isEmpty() else bar, True]
            self._show(e, vis)
            if not vis:
                continue
            shown += 1
            np_ = e[0]
            np_.setPosHpr(c.x, c.y, 0, math.degrees(c.heading) - 90, 0, 0)
            if e[1] is not None:
                e[1].setColor(blink[0], blink[1], blink[2], 1)
        for cid in [k for k in self.cars if k not in live]:
            self.cars.pop(cid)[0].removeNode()
        self.shown["cars"] = shown
        live, shown = set(), 0
        for p in self.world.peds:
            live.add(p.id)
            x, y = float(p.pos[0]), float(p.pos[1])
            vis = self._visible(x, y)
            e = self.peds.get(p.id)
            if e is None:
                if not vis:
                    continue
                np_ = self.ped_protos[p.id % len(self.ped_protos)].copyTo(self.root)
                np_.setScale(p.height / 1.75)
                e = self.peds[p.id] = [np_, np_.find("poses").node(), True]
            self._show(e, vis)
            if not vis:
                continue
            shown += 1
            e[0].setPosHpr(x, y, 0.02 if p.state == "cross" else 0.15, math.degrees(p.heading) - 90, 0, 0)
            k = int((p.anim % (2 * math.pi)) / (2 * math.pi) * self.POSES) % self.POSES if p.moving else self.POSES
            e[1].setVisibleChild(k)
        for pid in [k for k in self.peds if k not in live]:
            self.peds.pop(pid)[0].removeNode()
        self.shown["peds"] = shown


def setup_lights(render: NodePath) -> None:
    amb = AmbientLight("amb")
    amb.setColor(Vec4(0.5, 0.52, 0.56, 1))
    sun = DirectionalLight("sun")
    sun.setColor(Vec4(0.75, 0.72, 0.66, 1))
    sun_np = render.attachNewNode(sun)
    sun_np.setHpr(-35, -52, 0)
    render.setLight(render.attachNewNode(amb))
    render.setLight(sun_np)


def make_lamp_proto() -> NodePath:
    m = Mesh("lamp")
    m.sphere(0, 0, 0, 1.0, (1, 1, 1), seg=10, rings=6)
    np_ = m.node()
    np_.setLightOff()
    return np_


def update_lamps(lamps: list[Lamp], t: float) -> None:
    blink = (t % 1.0) < 0.5
    for lp in lamps:
        on = lamp_on(lp.color, lp.sc.groups[lp.group].state, blink)
        if on is not lp.lit:  # менять состояние сцены только при смене сигнала
            lp.lit = on
            col = LAMP_ON[lp.color] if on else LAMP_OFF[lp.color]
            lp.np.setColor(col[0], col[1], col[2], 1)
