"""Дорожная сеть участка из OpenStreetMap: улицы, полосы, манёвры, переходы, светофорные узлы.

Узел сети — перекрёсток, переход вне перекрёстка, смена параметров улицы или граница участка.
Ребро — участок улицы между узлами. Полосы строятся сдвигом оси улицы вправо (правостороннее движение).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

from emulator.config import BBOX, DATA, OBJECTS, ORIGIN, STREETS, ObjectSeed
from emulator.world.geom import Polyline, Projection, angle_diff, bezier, heading_deg, right_normal, unit

LANE_W = 3.5
CROSSWALK_W = 4.0
SIDEWALK_W = 4.0


@dataclass(eq=False)
class Node:
    id: int
    xy: np.ndarray
    arms: list = field(default_factory=list)   # [Arm]
    kind: str = "pass"                          # junction | crossing | pass | end
    radius: float = 0.0                         # радиус «коробки» перекрёстка
    signal: object = None                       # SignalController
    object_id: str | None = None                # объект пилота (оборудован камерами)
    axis_names: dict = field(default_factory=dict)

    @property
    def degree(self) -> int:
        return len(self.arms)


@dataclass(eq=False)
class Edge:
    id: int
    a: Node
    b: Node
    poly: Polyline
    name: str
    highway: str
    lanes_fwd: int
    lanes_bwd: int
    lanes: list = field(default_factory=list)

    @property
    def width(self) -> float:
        return (self.lanes_fwd + self.lanes_bwd) * LANE_W

    @property
    def half_left(self) -> float:   # от оси до левого края (встречные полосы)
        return self.lanes_bwd * LANE_W

    @property
    def half_right(self) -> float:
        return self.lanes_fwd * LANE_W


@dataclass(eq=False)
class Arm:
    """Выход ребра из узла."""
    node: Node
    edge: Edge
    at_start: bool          # узел — начало ребра
    bearing: float = 0.0    # азимут направления от узла по ребру
    axis: str = "A"
    trim: float = 0.0       # расстояние от узла до стоп-линии
    crosswalk: object = None

    def dir_away(self) -> np.ndarray:
        p = self.edge.poly
        return p.direction(0.0) if self.at_start else -p.direction(p.length)

    def point_at(self, d: float) -> np.ndarray:
        p = self.edge.poly
        return p.point(d) if self.at_start else p.point(p.length - d)

    def lanes_in(self) -> list:
        """Полосы, по которым машины подъезжают к узлу по этому рукаву."""
        return [l for l in self.edge.lanes if l.arm_in is self]

    def lanes_out(self) -> list:
        return [l for l in self.edge.lanes if l.from_node is self.node and l.forward == self.at_start]


@dataclass(eq=False)
class Lane:
    id: int
    edge: Edge
    forward: bool
    index: int                  # 0 — ближняя к оси (левая)
    count: int                  # полос в этом направлении
    poly: Polyline
    from_node: Node
    to_node: Node
    arm_in: Arm | None = None   # рукав узла to_node, по которому полоса подходит к узлу
    out: list = field(default_factory=list)
    cars: list = field(default_factory=list)
    siblings: list = field(default_factory=list)   # полосы этого направления по индексу, слева направо
    merge_requests: list = field(default_factory=list)  # машины из соседней полосы, которым нужно встроиться

    @property
    def ending(self) -> bool:
        """Полоса заканчивается (улица сужается): до конца нужно перестроиться."""
        return not self.out and self.to_node.kind != "end"

    @property
    def turns(self) -> set:
        return {c.turn for c in self.out}

    @property
    def length(self) -> float:
        return self.poly.length


@dataclass(eq=False)
class Connector:
    id: int
    from_lane: Lane
    to_lane: Lane
    poly: Polyline
    turn: str
    node: Node
    arm_out: Arm | None = None
    crosswalks: list = field(default_factory=list)
    cars: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)  # [(другой манёвр, уступаю ли я, моя зона, его зона)]
    reserved: float = -1.0                         # до какого времени манёвр занят въезжающей машиной
    merges: list = field(default_factory=list)     # манёвры, которые вливаются в ту же полосу

    @property
    def length(self) -> float:
        return self.poly.length


@dataclass(eq=False)
class Crosswalk:
    id: int
    node: Node
    arm: Arm | None
    center: np.ndarray
    along: np.ndarray       # направление перехода: от стороны 0 к стороне 1
    road_dir: np.ndarray    # направление улицы
    length: float           # ширина проезжей части
    group: str = "ped"
    peds: list = field(default_factory=list)

    def end(self, side: int) -> np.ndarray:
        sgn = -1 if side == 0 else 1
        return self.center + self.along * sgn * self.length / 2

    def contains(self, pt, margin: float = 0.0) -> bool:
        d = np.asarray(pt) - self.center
        return abs(float(np.dot(d, self.along))) <= self.length / 2 + margin and \
            abs(float(np.dot(d, self.road_dir))) <= CROSSWALK_W / 2 + margin


class Network:
    def __init__(self):
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.lanes: list[Lane] = []
        self.connectors: list[Connector] = []
        self.crosswalks: list[Crosswalk] = []
        self.buildings: list[dict] = []
        self.proj: Projection | None = None
        self.bounds = (0.0, 0.0, 0.0, 0.0)
        self.objects: dict[str, Node] = {}
        self.seeds: dict[str, ObjectSeed] = {}

    @property
    def signal_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.kind in ("junction", "crossing")]

    # ------------------------------------------------------------------ сборка
    @classmethod
    def load(cls, raw_path=DATA / "osm_lyublino_raw.json") -> "Network":
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        net = cls()
        net.proj = Projection(*ORIGIN)
        s, w, n, e = BBOX
        x0, y0 = net.proj.to_xy(s, w)
        x1, y1 = net.proj.to_xy(n, e)
        net.bounds = (x0, y0, x1, y1)
        net._build_graph(raw)
        net._attach_objects()
        net._merge_close_junctions()
        net._classify_nodes()
        net._build_lanes()
        net._build_connectors()
        net._build_crosswalks()
        net._load_buildings(raw)
        return net

    def _inside(self, xy) -> bool:
        x0, y0, x1, y1 = self.bounds
        return x0 <= xy[0] <= x1 and y0 <= xy[1] <= y1

    def _build_graph(self, raw: dict) -> None:
        coord: dict[int, np.ndarray] = {}
        segs: dict[frozenset, dict] = {}
        nbrs: dict[int, set] = {}
        for el in raw["elements"]:
            t = el.get("tags", {})
            if el["type"] != "way" or t.get("name") not in STREETS or t.get("highway") not in ("secondary", "tertiary"):
                continue
            ids, geo = el["nodes"], el["geometry"]
            for nid, g in zip(ids, geo):
                coord[nid] = np.array(self.proj.to_xy(g["lat"], g["lon"]))
            lanes = int(str(t.get("lanes", "2")).split(";")[0]) if str(t.get("lanes", "2")).split(";")[0].isdigit() else 2
            oneway = t.get("oneway") in ("yes", "1", "true")
            if oneway:
                fwd, bwd = max(1, lanes), 0
            else:
                fwd = int(t["lanes:forward"]) if str(t.get("lanes:forward", "")).isdigit() else max(1, math.ceil(lanes / 2))
                bwd = int(t["lanes:backward"]) if str(t.get("lanes:backward", "")).isdigit() else max(1, lanes - fwd)
            fwd, bwd = min(fwd, 3), min(bwd, 3)
            for a, b in zip(ids, ids[1:]):
                if not (self._inside(coord[a]) and self._inside(coord[b])):
                    continue
                segs[frozenset((a, b))] = {"a": a, "b": b, "name": t["name"], "hw": t["highway"], "fwd": fwd, "bwd": bwd}
                nbrs.setdefault(a, set()).add(b)
                nbrs.setdefault(b, set()).add(a)

        def attrs_key(seg, frm):
            fwd, bwd = (seg["fwd"], seg["bwd"]) if seg["a"] == frm else (seg["bwd"], seg["fwd"])
            return seg["name"], fwd, bwd

        key_nodes = set()
        for nid, ns in nbrs.items():
            if len(ns) != 2:
                key_nodes.add(nid)
            else:
                p, q = list(ns)
                s1, s2 = segs[frozenset((p, nid))], segs[frozenset((nid, q))]
                if attrs_key(s1, p) != attrs_key(s2, nid):
                    key_nodes.add(nid)
        # замкнутые кольца без ключевых узлов не нужны; обходим от ключевых
        node_obj: dict[int, Node] = {}

        def get_node(nid):
            if nid not in node_obj:
                node_obj[nid] = Node(len(self.nodes), coord[nid].copy())
                self.nodes.append(node_obj[nid])
            return node_obj[nid]

        used = set()
        for k in key_nodes:
            for n0 in nbrs[k]:
                if frozenset((k, n0)) in used:
                    continue
                path, prev, cur = [k], k, n0
                first = segs[frozenset((k, n0))]
                used.add(frozenset((k, n0)))
                while True:
                    path.append(cur)
                    if cur in key_nodes:
                        break
                    nxt = [x for x in nbrs[cur] if x != prev][0]
                    used.add(frozenset((cur, nxt)))
                    prev, cur = cur, nxt
                fwd, bwd = (first["fwd"], first["bwd"]) if first["a"] == k else (first["bwd"], first["fwd"])
                poly = Polyline([coord[i] for i in path])
                if poly.length < 1.0:
                    continue
                self.edges.append(Edge(len(self.edges), get_node(k), get_node(cur), poly, first["name"], first["hw"], fwd, bwd))

    def _split_edge(self, edge: Edge, s: float) -> Node:
        node = Node(len(self.nodes), edge.poly.point(s))
        self.nodes.append(node)
        e2 = Edge(len(self.edges), node, edge.b, edge.poly.sub(s, edge.poly.length), edge.name, edge.highway,
                  edge.lanes_fwd, edge.lanes_bwd)
        self.edges.append(e2)
        edge.poly, edge.b = edge.poly.sub(0, s), node
        return node

    def _attach_objects(self) -> None:
        for seed in OBJECTS:
            self.seeds[seed.id] = seed
            xy = np.array(self.proj.to_xy(seed.lat, seed.lon))
            if seed.kind == "crossing":
                best = min(((e, *e.poly.project(xy)) for e in self.edges), key=lambda t: t[2])
                edge, s, dist = best
                if dist > 30:
                    raise ValueError(f"{seed.id}: переход в {dist:.0f} м от улиц сети")
                s = min(max(s, 12.0), edge.poly.length - 12.0)
                node = self._split_edge(edge, s)
                node.kind = "crossing"
            else:
                degs = self._degrees()
                cand = [n for n in self.nodes if degs.get(n.id, 0) >= 3]
                node = min(cand, key=lambda n: float(np.hypot(*(n.xy - xy))))
                if float(np.hypot(*(node.xy - xy))) > 40:
                    raise ValueError(f"{seed.id}: перекрёсток не найден рядом с точкой")
            node.object_id = seed.id
            self.objects[seed.id] = node

    def _degrees(self) -> dict[int, int]:
        d: dict[int, int] = {}
        for e in self.edges:
            d[e.a.id] = d.get(e.a.id, 0) + 1
            d[e.b.id] = d.get(e.b.id, 0) + 1
        return d

    def _merge_close_junctions(self) -> None:
        """Раздельные проезжие части дают несколько близких узлов на одном перекрёстке — объединяем."""
        degs = self._degrees()
        parent = {n.id: n.id for n in self.nodes}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for e in self.edges:
            if e.poly.length < 25 and degs.get(e.a.id, 0) >= 3 and degs.get(e.b.id, 0) >= 3 \
                    and "crossing" not in (e.a.kind, e.b.kind):
                parent[find(e.a.id)] = find(e.b.id)
        groups: dict[int, list[Node]] = {}
        for n in self.nodes:
            groups.setdefault(find(n.id), []).append(n)
        remap = {}
        for members in groups.values():
            keep = next((m for m in members if m.object_id), members[0])
            if len(members) > 1:
                keep.xy = np.mean([m.xy for m in members], axis=0)
            for m in members:
                remap[m.id] = keep
        self.edges = [e for e in self.edges if remap[e.a.id] is not remap[e.b.id]]
        for e in self.edges:
            a, b = remap[e.a.id], remap[e.b.id]
            if a is not e.a or b is not e.b or len(groups[find(a.id)]) > 1 or len(groups[find(b.id)]) > 1:
                pts = e.poly.p.copy()
                pts[0], pts[-1] = a.xy, b.xy
                e.poly = Polyline(pts)
            e.a, e.b = a, b
        live = {e.a.id for e in self.edges} | {e.b.id for e in self.edges}
        self.nodes = [n for n in self.nodes if n.id in live]
        for i, n in enumerate(self.nodes):
            n.id = i
        for i, e in enumerate(self.edges):
            e.id = i

    def _classify_nodes(self) -> None:
        for e in self.edges:
            e.a.arms.append(Arm(e.a, e, True))
            e.b.arms.append(Arm(e.b, e, False))
        for n in self.nodes:
            for arm in n.arms:
                arm.bearing = heading_deg(arm.dir_away())
            if n.kind == "crossing":
                pass
            elif n.degree >= 3:
                n.kind = "junction"
            elif n.degree == 1:
                n.kind = "end"
            else:
                n.kind = "pass"
            if n.kind == "junction":
                n.radius = max(max(a.edge.half_left, a.edge.half_right) for a in n.arms) + 1.5
                # главная ось — пара рукавов, ближе всего к противоположным направлениям
                best, pair = 1e9, None
                for i in range(n.degree):
                    for j in range(i + 1, n.degree):
                        dev = abs(abs(angle_diff(n.arms[i].bearing, n.arms[j].bearing)) - 180)
                        w = dev - 0.5 * (n.arms[i].edge.width + n.arms[j].edge.width) / LANE_W
                        if w < best:
                            best, pair = w, (i, j)
                axis_a = n.arms[pair[0]].bearing % 180
                for arm in n.arms:
                    d = abs(angle_diff(axis_a, arm.bearing % 180))
                    arm.axis = "A" if min(d, 180 - d) < 45 else "B"
                for ax in ("A", "B"):
                    names = [a.edge.name for a in n.arms if a.axis == ax]
                    n.axis_names[ax] = max(set(names), key=names.count) if names else ""
                for arm in n.arms:
                    arm.trim = n.radius + CROSSWALK_W + 1.5
            elif n.kind == "crossing":
                for arm in n.arms:
                    arm.trim = CROSSWALK_W / 2 + 1.5
                    arm.axis = "A"
                n.axis_names["A"] = n.arms[0].edge.name
            elif n.kind == "pass":
                for arm in n.arms:
                    arm.trim = 0.5

    def _build_lanes(self) -> None:
        for e in self.edges:
            trim_a = next(a.trim for a in e.a.arms if a.edge is e and a.at_start)
            trim_b = next(a.trim for a in e.b.arms if a.edge is e and not a.at_start)
            if e.poly.length - trim_a - trim_b < 4:
                trim_a = trim_b = max(0.3, (e.poly.length - 4) / 2)
            for forward, count in ((True, e.lanes_fwd), (False, e.lanes_bwd)):
                base = e.poly if forward else e.poly.reversed()
                t0, t1 = (trim_a, trim_b) if forward else (trim_b, trim_a)
                for i in range(count):
                    poly = base.offset((i + 0.5) * LANE_W).sub(t0, base.length - t1)
                    frm, to = (e.a, e.b) if forward else (e.b, e.a)
                    lane = Lane(len(self.lanes), e, forward, i, count, poly, frm, to)
                    lane.arm_in = next(a for a in to.arms if a.edge is e and a.at_start == (not forward))
                    e.lanes.append(lane)
                    self.lanes.append(lane)

    RANK = {"straight": 0, "right": 1, "left": 2}  # кто кому уступает: поворот налево — всем

    def _connect(self, lane: Lane, tgt: Lane, turn: str, n: Node, arm_out: Arm) -> None:
        p0, p1 = lane.poly.point(lane.length), tgt.poly.point(0)
        h0, h1 = unit(lane.poly.direction(lane.length)), unit(tgt.poly.direction(0))
        if turn == "straight" and n.degree <= 2 and tgt.index == lane.index:
            poly = Polyline([p0, p1])
        else:
            poly = bezier(p0, h0, p1, h1)
        c = Connector(len(self.connectors), lane, tgt, poly, turn, n, arm_out)
        lane.out.append(c)
        self.connectors.append(c)

    def _build_connectors(self) -> None:
        """Манёвры по разметке: левая полоса — налево, правая — направо, средние — прямо.
        На двух полосах: левая «налево и прямо», правая «прямо и направо». Где улица сужается,
        лишние полосы заканчиваются, и машины перестраиваются заранее."""
        for e in self.edges:
            for fwd in (True, False):
                sib = sorted((l for l in e.lanes if l.forward == fwd), key=lambda l: l.index)
                for l in sib:
                    l.siblings = sib
        for n in self.nodes:
            if n.kind == "end":
                continue
            for arm_in in n.arms:
                ins = sorted(arm_in.lanes_in(), key=lambda l: l.index)
                if not ins:
                    continue
                nin = len(ins)
                h_in = unit(ins[0].poly.direction(ins[0].length))
                moves = []
                for arm in n.arms:
                    if arm is arm_in:
                        continue
                    outs = sorted(arm.lanes_out(), key=lambda l: l.index)
                    if not outs:
                        continue
                    if n.degree <= 2:
                        turn = "straight"
                    else:
                        dev = angle_diff(heading_deg(h_in), arm.bearing)
                        if abs(dev) > 150:
                            continue  # разворот
                        turn = "straight" if abs(dev) < 35 else ("left" if dev < 0 else "right")
                    moves.append((turn, arm, outs))
                if not moves:
                    continue
                if n.degree <= 2:  # продолжение улицы: полоса в полосу, лишние заканчиваются
                    _t, arm, outs = moves[0]
                    m = len(outs)
                    for l in ins:
                        targets = [outs[l.index]] if l.index < m else []
                        if l.index == nin - 1 and m > nin:
                            targets += outs[nin:]  # улица расширяется: крайняя полоса питает новые
                        for tgt in targets:
                            self._connect(l, tgt, "straight", n, arm)
                    continue
                turns = {t for t, _, _ in moves}
                allowed = {l.index: set() for l in ins}
                if nin == 1:
                    allowed[0] = set(turns)
                else:
                    for l in ins:
                        i = l.index
                        if i == 0 and "left" in turns:
                            allowed[i].add("left")
                        if i == nin - 1 and "right" in turns:
                            allowed[i].add("right")
                        if "straight" in turns and (nin == 2 or 0 < i < nin - 1):
                            allowed[i].add("straight")
                    for l in ins:  # полоса без манёвра (например, средняя на Т-образном): налево или направо
                        if not allowed[l.index]:
                            want = "left" if l.index < nin / 2 else "right"
                            allowed[l.index].add(want if want in turns else next(iter(turns)))
                    for t in turns:  # у каждого манёвра должна быть полоса
                        if not any(t in a_ for a_ in allowed.values()):
                            allowed[0 if t == "left" else nin - 1 if t == "right" else nin // 2].add(t)
                for turn, arm, outs in moves:
                    lanes_t = [l for l in ins if turn in allowed[l.index]]
                    m = len(outs)
                    if turn == "right":
                        pairs = list(zip(reversed(lanes_t), reversed(outs)))
                    elif turn == "left":
                        pairs = list(zip(lanes_t, outs))
                    else:
                        off = max(0, (m - len(lanes_t)) // 2)
                        pairs = list(zip(lanes_t, outs[off:]))
                    mapped = {id(l) for l, _ in pairs}
                    for l in lanes_t:  # полосу, которой не хватило своего выезда, ведём в ближайший
                        if id(l) not in mapped:
                            pairs.append((l, outs[-1] if turn == "right" else outs[0] if turn == "left"
                                          else outs[min(l.index, m - 1)]))
                    for l, tgt in pairs:
                        self._connect(l, tgt, turn, n, arm)
        self._find_conflicts()

    ZONE_DIST = 2.4  # оси траекторий ближе этого — кузова могут задеть друг друга

    def _find_conflicts(self) -> None:
        """Пары манёвров одного узла, траектории которых сближаются меньше ZONE_DIST (пересечение,
        слияние в одну полосу, сближение на повороте). Для каждой пары — зона конфликта на обеих
        траекториях: (начало, конец) в метрах от начала манёвра."""
        samples = {}
        for c in self.connectors:
            n = max(2, int(c.length / 0.5) + 1)
            ss = np.linspace(0, c.length, n)
            samples[c.id] = (ss, np.array([c.poly.point(x) for x in ss]))
        by_node: dict[int, list] = {}
        for c in self.connectors:
            by_node.setdefault(c.node.id, []).append(c)
        for cs in by_node.values():
            for i, a in enumerate(cs):
                sa, pa = samples[a.id]
                for b in cs[i + 1:]:
                    if a.from_lane is b.from_lane:
                        continue
                    sb, pb = samples[b.id]
                    d = np.hypot(pa[:, None, 0] - pb[None, :, 0], pa[:, None, 1] - pb[None, :, 1])
                    close = d < self.ZONE_DIST
                    if not close.any():
                        continue
                    ia, ib = np.nonzero(close.any(axis=1))[0], np.nonzero(close.any(axis=0))[0]
                    za = (float(sa[ia.min()]), float(sa[ia.max()]))
                    zb = (float(sb[ib.min()]), float(sb[ib.max()]))
                    if a.to_lane is b.to_lane:  # слияние: «застёжка», см. World._merge_gap
                        a.merges.append(b)
                        b.merges.append(a)
                        continue
                    ra, rb = self.RANK[a.turn], self.RANK[b.turn]
                    a.conflicts.append((b, ra > rb, za, zb))
                    b.conflicts.append((a, rb > ra, zb, za))

    def _build_crosswalks(self) -> None:
        for n in self.nodes:
            if n.kind == "crossing":
                arm = n.arms[0]
                road = arm.dir_away()
                along = right_normal(road)
                e = arm.edge
                cw = Crosswalk(len(self.crosswalks), n, None, n.xy.copy(), along, unit(road), e.width, "ped")
                # сдвиг центра: ось улицы не посередине, если полос в стороны разное число
                shift = (e.half_right - e.half_left) / 2 * (1 if arm.at_start else -1)
                cw.center = n.xy + along * shift
                self.crosswalks.append(cw)
                for a in n.arms:
                    a.crosswalk = cw
            elif n.kind == "junction":
                for arm in n.arms:
                    e = arm.edge
                    dist = n.radius + CROSSWALK_W / 2 + 0.5
                    if dist > e.poly.length / 2:
                        continue
                    road = unit(arm.dir_away())
                    along = right_normal(road)
                    center = arm.point_at(dist)
                    # полосы от узла — справа от направления «от узла»
                    right = e.half_right if arm.at_start else e.half_left
                    left = e.half_left if arm.at_start else e.half_right
                    center = center + along * (right - left) / 2
                    group = "ped_" + arm.axis
                    cw = Crosswalk(len(self.crosswalks), n, arm, center, along, road, e.width, group)
                    arm.crosswalk = cw
                    self.crosswalks.append(cw)
        for c in self.connectors:
            n = c.node
            if n.kind == "crossing":
                cws = [n.arms[0].crosswalk]
            elif n.kind == "junction":
                arm_out = next(a for a in n.arms if a.edge is c.to_lane.edge and a.at_start == c.to_lane.forward)
                cws = [x for x in (c.from_lane.arm_in.crosswalk, arm_out.crosswalk) if x is not None]
            else:
                cws = []
            # где траектория машины пересекает переход (координата вдоль перехода)
            c.crosswalks = []
            for cw in cws:
                pts = np.array([c.poly.point(x) for x in np.linspace(0, c.length, 24)])
                ends = np.array([c.from_lane.poly.point(max(0.0, c.from_lane.length - 6)), c.to_lane.poly.point(min(6.0, c.to_lane.length))])
                allp = np.vstack([ends[:1], pts, ends[1:]])
                d = np.abs((allp - cw.center) @ cw.road_dir)
                k = int(np.argmin(d))
                c.crosswalks.append((cw, float((allp[k] - cw.center) @ cw.along)))

    def _load_buildings(self, raw: dict) -> None:
        x0, y0, x1, y1 = self.bounds
        for el in raw["elements"]:
            t = el.get("tags", {})
            if el["type"] != "way" or "building" not in t or "geometry" not in el:
                continue
            pts = [self.proj.to_xy(g["lat"], g["lon"]) for g in el["geometry"]]
            if pts[0] == pts[-1]:
                pts = pts[:-1]
            if len(pts) < 3:
                continue
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            if not (x0 - 60 <= cx <= x1 + 60 and y0 - 60 <= cy <= y1 + 60):
                continue
            levels = t.get("building:levels", "")
            try:
                h = float(levels) * 3.0 if levels else None
            except ValueError:
                h = None
            if h is None:
                h = {"garages": 3.0, "garage": 3.0, "kiosk": 3.0, "retail": 6.0}.get(t["building"], None)
            if h is None:
                h = 3.0 * (5 + (el["id"] % 8))  # нет этажности в OSM: 5–12 этажей
            self.buildings.append({"pts": pts, "height": h, "kind": t["building"], "id": el["id"],
                                   "address": " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)})

    # ------------------------------------------------------------------ сводка
    def describe(self) -> str:
        kinds: dict[str, int] = {}
        for n in self.nodes:
            kinds[n.kind] = kinds.get(n.kind, 0) + 1
        return (f"узлов {len(self.nodes)} {kinds}, рёбер {len(self.edges)}, полос {len(self.lanes)}, "
                f"манёвров {len(self.connectors)}, переходов {len(self.crosswalks)}, домов {len(self.buildings)}")
