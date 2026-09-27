"""Восприятие объекта: детекции двух камер → треки на земле → наблюдение для контроллера и события.

Детекции каждой камеры переводятся в метры по калибровке, совпадающие точки двух камер
сливаются, треки ведутся на плоскости земли (не в пикселях), поэтому пешеход, которого видят
обе камеры, считается один раз, а время ожидания не сбрасывается при переходе между камерами.
"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from node.model import Layout, Observation, Ped
from node.params import Params
from node.vision.detector import PERSON, VEHICLE, Detection
from node.vision.geometry import CameraModel, SiteGeometry

GATE = {PERSON: 2.0, VEHICLE: 5.0}      # максимальный скачок трека между обновлениями, м
MERGE = {PERSON: 1.2, VEHICLE: 3.0}     # детекции двух камер ближе этого — один объект, м
TRACK_TTL_S = 1.6
WAIT_DWELL_S = 2.0                      # столько стоять у перехода, чтобы считаться ждущим
STOPPED_MPS = 1.5


@dataclass
class Track:
    id: int
    kind: str
    pos: np.ndarray
    t_first: float
    t_last: float
    vel: np.ndarray = field(default_factory=lambda: np.zeros(2))
    hits: int = 1
    beacon: float = 0.0
    # пешеход
    cw: str | None = None
    zone: str | None = None
    zone_since: float = 0.0
    wait_start: float | None = None
    waiting: bool = False
    # машина
    approach: int | None = None
    s: float | None = None
    stopped: bool = False

    @property
    def speed(self) -> float:
        return float(np.hypot(*self.vel))


@dataclass
class Update:
    obs: Observation
    passed: dict[str, int] = field(default_factory=dict)
    stopped: dict[str, int] = field(default_factory=dict)
    arrived: dict[str, int] = field(default_factory=dict)
    served: list[tuple[str, float, bool]] = field(default_factory=list)


class Perception:
    def __init__(self, site: dict, layout: Layout, cams: list[CameraModel], params: Params):
        self.geo = SiteGeometry(site)
        self.L = layout
        self.p = params
        self.cams = {c.id: c for c in cams}
        self.coverage = self.geo.coverage(cams)
        self.inbox: deque = deque(maxlen=64)
        self.tracks: list[Track] = []
        self._ids = itertools.count(1)
        self.last_ts = 0.0
        self.overlay: dict[str, tuple] = {}   # камера -> (кадр, детекции) для видео с разметкой
        self.lock = threading.Lock()
        self.cw_group = {cw.id: cw.group for cw in self.geo.crosswalks}

    # вызывается из потока детектора
    def push(self, cam_id: str, frame, dets: list[Detection]) -> None:
        self.inbox.append((cam_id, frame.ts, dets))
        with self.lock:
            self.overlay[cam_id] = (frame, dets)

    def _measurements(self, healthy: set[str]) -> tuple[float | None, list[tuple[str, np.ndarray, bool]]]:
        latest: dict[str, tuple[float, list[Detection]]] = {}
        while self.inbox:
            cid, ts, dets = self.inbox.popleft()
            if cid in healthy and (cid not in latest or ts >= latest[cid][0]):
                latest[cid] = (ts, dets)
        if not latest:
            return None, []
        ts = max(t for t, _ in latest.values())
        meas: list[tuple[str, np.ndarray, bool]] = []
        for cid, (_, dets) in latest.items():
            if not dets:
                continue
            ground = self.cams[cid].pixels_to_ground(np.array([d.foot for d in dets]))
            for d, g in zip(dets, ground):
                if not np.isnan(g[0]):
                    meas.append((d.kind, g, d.beacon))
        # слияние детекций одного объекта с двух камер
        merged: list[tuple[str, np.ndarray, bool]] = []
        used = [False] * len(meas)
        for i, (k, p, b) in enumerate(meas):
            if used[i]:
                continue
            group = [p]
            beacon = b
            for j in range(i + 1, len(meas)):
                if not used[j] and meas[j][0] == k and np.hypot(*(meas[j][1] - p)) < MERGE[k]:
                    used[j] = True
                    group.append(meas[j][1])
                    beacon = beacon or meas[j][2]
            merged.append((k, np.mean(group, axis=0), beacon))
        return ts, merged

    def update(self, healthy: set[str], signals: dict[str, str]) -> Update | None:
        ts, meas = self._measurements(healthy)
        if ts is None:
            return None
        up = Update(Observation())
        dt = max(0.05, ts - self.last_ts) if self.last_ts else 0.3
        self.last_ts = ts
        # сопоставление с треками: жадно по расстоянию до предсказанной позиции
        pairs = []
        for mi, (k, p, _) in enumerate(meas):
            for ti, tr in enumerate(self.tracks):
                if tr.kind == k:
                    d = float(np.hypot(*(p - (tr.pos + tr.vel * dt))))
                    if d < GATE[k] + (tr.speed * 0.3 if k == VEHICLE else 0):
                        pairs.append((d, mi, ti))
        pairs.sort()
        mt, tt = set(), set()
        for d, mi, ti in pairs:
            if mi in mt or ti in tt:
                continue
            mt.add(mi)
            tt.add(ti)
            tr = self.tracks[ti]
            k, p, beacon = meas[mi]
            new_vel = (p - tr.pos) / dt
            tr.vel = 0.6 * tr.vel + 0.4 * new_vel if tr.hits > 1 else new_vel * 0.5
            tr.pos = 0.5 * (tr.pos + tr.vel * dt) + 0.5 * p
            tr.t_last = ts
            tr.hits += 1
            tr.beacon = 0.8 * tr.beacon + 0.2 * float(beacon)
        for mi, (k, p, beacon) in enumerate(meas):
            if mi not in mt:
                self.tracks.append(Track(next(self._ids), k, p.copy(), ts, ts, beacon=float(beacon)))
        self.tracks = [t for t in self.tracks if ts - t.t_last <= TRACK_TTL_S]
        for tr in self.tracks:
            if tr.hits >= 2:
                if tr.kind == PERSON:
                    self._ped(tr, ts, signals, up)
                else:
                    self._veh(tr, up)
        up.obs = self._observation(ts, healthy)
        return up

    def _ped(self, tr: Track, ts: float, signals: dict[str, str], up: Update) -> None:
        cw, zone, _side = self.geo.crosswalk_at(tr.pos)
        cw_id = cw.id if cw else None
        if (cw_id, zone) != (tr.cw, tr.zone):
            if tr.waiting and zone == "cross" and cw_id == tr.cw and tr.wait_start is not None:
                g = self.cw_group[cw_id]
                red = signals.get(g) not in (Ped.GREEN, Ped.GREEN_BLINK)
                up.served.append((g, ts - tr.wait_start, red))
            if zone != "wait":
                tr.waiting, tr.wait_start = False, None
            tr.cw, tr.zone, tr.zone_since = cw_id, zone, ts
        if zone == "wait":
            dwell = ts - tr.zone_since
            if not tr.waiting and (dwell >= WAIT_DWELL_S or (dwell >= 0.6 and tr.speed < 0.4)):
                tr.waiting, tr.wait_start = True, tr.zone_since
                g = self.cw_group[cw_id]
                up.arrived[g] = up.arrived.get(g, 0) + 1

    def _veh(self, tr: Track, up: Update) -> None:
        ap, s = self.geo.approach_at(tr.pos)
        idx = self.geo.approaches.index(ap) if ap else None
        if tr.approach is not None and tr.s is not None:
            prev = self.geo.approaches[tr.approach]
            # проехал стоп-линию к центру: внутри подхода или вышел из зоны подхода у стоп-линии
            crossed = (idx == tr.approach and tr.s > prev.stop_s >= s) or                       (idx != tr.approach and prev.stop_s - 0.5 <= tr.s < prev.stop_s + 8 and tr.speed >= STOPPED_MPS)
            if crossed:
                up.passed[prev.group] = up.passed.get(prev.group, 0) + 1
                if tr.stopped:
                    up.stopped[prev.group] = up.stopped.get(prev.group, 0) + 1
        if idx is not None and idx == tr.approach and tr.s is not None:
            if s > ap.stop_s and tr.speed < STOPPED_MPS:
                tr.stopped = True
        if idx != tr.approach:
            tr.stopped = False
        tr.approach, tr.s = idx, (s if ap else None)

    def _observation(self, ts: float, healthy: set[str]) -> Observation:
        vis: set[str] = set()
        for cid in healthy:
            vis |= self.coverage.get(cid, set())
        o = Observation(cameras={c: c in healthy for c in self.cams})
        for g in self.L.ped_groups():
            if g not in vis:
                o.waiting[g] = o.max_wait[g] = o.wait_sum[g] = o.on_crosswalk[g] = None
                continue
            waiting = [t for t in self.tracks if t.kind == PERSON and t.waiting and t.cw and self.cw_group[t.cw] == g]
            o.waiting[g] = len(waiting)
            o.max_wait[g] = max((ts - t.wait_start for t in waiting), default=0.0)
            o.wait_sum[g] = sum(ts - t.wait_start for t in waiting)
            o.on_crosswalk[g] = sum(1 for t in self.tracks if t.kind == PERSON and t.zone == "cross"
                                    and t.cw and self.cw_group[t.cw] == g)
        for g in self.L.veh_groups():
            if g not in vis:
                o.queue[g] = o.eta[g] = None
                continue
            q, eta = 0, None
            for t in self.tracks:
                if t.kind != VEHICLE or t.approach is None or t.hits < 2:
                    continue
                ap = self.geo.approaches[t.approach]
                if ap.group != g or t.s is None:
                    continue
                gap = t.s - ap.stop_s
                if t.speed < STOPPED_MPS and -2 < gap < 60:
                    q += 1
                elif t.speed >= STOPPED_MPS and 0 < gap < 80:
                    toward = -np.asarray(ap.pts[1] - ap.pts[0])
                    toward = toward / (np.hypot(*toward) or 1)
                    if float(np.dot(t.vel, toward)) > 0.5 * t.speed:
                        e = gap / t.speed
                        eta = e if eta is None else min(eta, e)
                if t.beacon > 0.3 and gap < 150:
                    o.emergency.add(g)
            o.queue[g], o.eta[g] = q, eta
        return o

    def refresh(self, ts: float, healthy: set[str]) -> Observation:
        """Новых кадров нет: убрать устаревшие треки и пересчитать наблюдение (например, камеры отказали)."""
        self.tracks = [t for t in self.tracks if ts - t.t_last <= TRACK_TTL_S]
        return self._observation(ts, healthy)

    def snapshot_overlay(self, cam_id: str):
        with self.lock:
            return self.overlay.get(cam_id)
