"""Zone analytics: turn tracked detections into counts per zone.

Zones are stored in normalized coordinates (0..1), so they survive a change of
camera resolution. Objects are located by their ground contact point (bottom
center of the bbox), which matters for perspective views.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from smartcross.config import ZoneConfig

PEDESTRIAN = "pedestrian"
VEHICLE = "vehicle"
BICYCLE = "bicycle"

COCO_CATEGORY = {
    "person": PEDESTRIAN,
    "bicycle": BICYCLE,
    "car": VEHICLE,
    "motorcycle": VEHICLE,
    "bus": VEHICLE,
    "truck": VEHICLE,
}


@dataclass
class Detection:
    track_id: int | None
    cls: str                 # raw class name, e.g. "car"
    category: str            # PEDESTRIAN / VEHICLE / BICYCLE
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels
    conf: float
    emergency: bool = False

    def foot(self) -> tuple[float, float]:
        x1, _, x2, y2 = self.box
        return (x1 + x2) / 2, y2


def point_in_polygon(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    """Ray casting; poly in the same coordinate system as pt."""
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def side_of_line(pt, a, b) -> float:
    return (b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0])


def segments_intersect(p1, p2, a, b) -> bool:
    d1, d2 = side_of_line(p1, a, b), side_of_line(p2, a, b)
    d3, d4 = side_of_line(a, p1, p2), side_of_line(b, p1, p2)
    # a point landing exactly on the line counts as crossed; leaving it doesn't count twice
    return d1 != 0 and d1 * d2 <= 0 and d3 * d4 <= 0


@dataclass
class ZoneReport:
    ped_waiting: dict[str, int] = field(default_factory=dict)   # side -> count
    ped_on_crossing: int = 0
    veh_in_approach: dict[str, int] = field(default_factory=dict)  # direction -> count
    approach_length_m: dict[str, float] = field(default_factory=dict)
    veh_passed: dict[str, int] = field(default_factory=dict)    # direction -> new vehicles counted this frame
    veh_passed_by_class: dict[str, int] = field(default_factory=dict)
    ped_new: int = 0                                            # new unique pedestrians on the crossing
    emergency: bool = False
    persons_total: int = 0
    vehicles_total: int = 0


class ZoneAnalyzer:
    """Stateful per camera: remembers track positions for line crossing and unique counting."""

    PED_MIN_HITS = 3

    def __init__(self, zones: list[ZoneConfig]):
        self.zones = zones
        self.prev_foot: dict[int, tuple[tuple[float, float], int]] = {}  # tid -> (foot, frame)
        self.counted: dict[str, set[int]] = {}       # line/zone id -> counted track ids
        self.in_approach_prev: dict[str, set[int]] = {}
        self.ped_seen: set[int] = set()
        self.ped_hits: dict[int, int] = {}
        self._frame = 0

    def analyze(self, dets: list[Detection], width: int, height: int) -> ZoneReport:
        self._frame += 1
        rep = ZoneReport()
        scale = np.array([width, height], dtype=float)
        polys = {z.id: [tuple(np.array(p) * scale) for p in z.points] for z in self.zones}
        has_lines = {z.direction for z in self.zones if z.type == "count_line"}
        cur_foot: dict[int, tuple[float, float]] = {}

        for z in self.zones:
            if z.type == "ped_wait":
                rep.ped_waiting.setdefault(z.side or z.id, 0)
            elif z.type == "approach":
                rep.veh_in_approach.setdefault(z.direction or z.id, 0)
                rep.approach_length_m[z.direction or z.id] = (
                    rep.approach_length_m.get(z.direction or z.id, 0) + (z.length_m or 0))

        in_approach_now: dict[str, set[int]] = {}
        for d in dets:
            foot = d.foot()
            if d.track_id is not None:
                cur_foot[d.track_id] = foot
            if d.category == PEDESTRIAN:
                rep.persons_total += 1
            elif d.category == VEHICLE:
                rep.vehicles_total += 1
                rep.emergency |= d.emergency
            for z in self.zones:
                if z.type == "count_line":
                    continue
                if not point_in_polygon(foot, polys[z.id]):
                    continue
                if z.type == "ped_wait" and d.category == PEDESTRIAN:
                    rep.ped_waiting[z.side or z.id] += 1
                elif z.type == "crosswalk" and d.category == PEDESTRIAN:
                    rep.ped_on_crossing += 1
                    # count a pedestrian once, after the track is stable (filters ID switches / flicker)
                    if d.track_id is not None and d.track_id not in self.ped_seen:
                        hits = self.ped_hits.get(d.track_id, 0) + 1
                        self.ped_hits[d.track_id] = hits
                        if hits >= self.PED_MIN_HITS:
                            self.ped_seen.add(d.track_id)
                            rep.ped_new += 1
                elif z.type == "approach" and d.category == VEHICLE:
                    key = z.direction or z.id
                    rep.veh_in_approach[key] += 1
                    if d.track_id is not None:
                        in_approach_now.setdefault(z.id, set()).add(d.track_id)

            # count lines: vehicle foot point trajectory crosses the line
            if d.category == VEHICLE and d.track_id is not None and d.track_id in self.prev_foot:
                prev = self.prev_foot[d.track_id][0]
                for z in self.zones:
                    if z.type != "count_line" or len(z.points) < 2:
                        continue
                    a, b = polys[z.id][0], polys[z.id][1]
                    done = self.counted.setdefault(z.id, set())
                    if d.track_id not in done and segments_intersect(prev, foot, a, b):
                        done.add(d.track_id)
                        key = z.direction or z.id
                        rep.veh_passed[key] = rep.veh_passed.get(key, 0) + 1
                        rep.veh_passed_by_class[d.cls] = rep.veh_passed_by_class.get(d.cls, 0) + 1

        # Fallback when a direction has no count line: a vehicle leaving an approach zone is counted.
        for z in self.zones:
            if z.type != "approach" or z.direction in has_lines:
                continue
            left = self.in_approach_prev.get(z.id, set()) - in_approach_now.get(z.id, set())
            done = self.counted.setdefault(z.id, set())
            for tid in left - done:
                done.add(tid)
                key = z.direction or z.id
                rep.veh_passed[key] = rep.veh_passed.get(key, 0) + 1
                rep.veh_passed_by_class["vehicle"] = rep.veh_passed_by_class.get("vehicle", 0) + 1
        self.in_approach_prev = in_approach_now

        # keep positions of briefly lost tracks, so a missed frame doesn't miss a line crossing
        for tid, f in cur_foot.items():
            self.prev_foot[tid] = (f, self._frame)
        self.prev_foot = {k: v for k, v in self.prev_foot.items() if self._frame - v[1] <= 30}
        # bound memory: forget old ids periodically
        if self._frame % 500 == 0:
            live = set(self.prev_foot)
            self.ped_seen &= live
            self.ped_hits = {k: v for k, v in self.ped_hits.items() if k in live}
            for k in self.counted:
                self.counted[k] &= live
        return rep
