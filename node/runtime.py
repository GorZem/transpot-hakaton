"""Узел объекта: источник данных → наблюдение → режим → контроллер → безопасность → светофор.

Один экземпляр на объект. Может работать внутри центра (режим «центр») или отдельным процессом
на встраиваемом ПК у светофора (режим «узел»): интерфейс одинаковый — tick() и snapshot().
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime

from node.control import Controller
from node.flow import FlowEstimator
from node.model import MODE_TITLES, Layout, Mode, Observation, Ped, Veh, build_layout
from node.params import Params
from node.safety import Safety
from node.synthetic import World


@dataclass
class MinuteStats:
    ped_arrived: int = 0
    ped_served: int = 0
    wait_sum: float = 0.0
    wait_max: float = 0.0
    violations: int = 0
    groups: int = 0
    ped_phases: int = 0
    veh_passed: int = 0
    veh_stopped: int = 0
    mode_s: dict[str, float] = field(default_factory=dict)


@dataclass
class SiteEvent:
    ts: datetime
    level: str
    kind: str
    message: str


def _seed(site_id: str) -> int:
    return int(hashlib.md5(site_id.encode()).hexdigest()[:8], 16)


def default_demand(layout: Layout, site: dict, seed: int) -> tuple[dict, dict]:
    """Пиковая интенсивность по подходам и поток пешеходов для тестового источника."""
    import random
    r = random.Random(seed)
    veh = {}
    for g in layout.veh_groups():
        veh[g] = r.uniform(180, 260) if g == "st" else r.uniform(420, 620)
    ped = {}
    for g in layout.ped_groups():
        ped[g] = r.uniform(3.0, 6.0) if layout.kind == "crossing" else r.uniform(1.5, 3.5)
    return veh, ped


class SiteRuntime:
    def __init__(self, site: dict, params: Params | None = None, now: float = 0.0):
        self.site = site
        self.id = site["id"]
        self.p = params or Params()
        self.layout = build_layout(site["kind"], site.get("road_bearing_deg", 0.0))
        self.ctrl = Controller(self.layout, self.p, now)
        self.safety = Safety(self.layout, self.p)
        seed = _seed(self.id)
        veh, ped = default_demand(self.layout, site, seed)
        self.world = World(self.layout, veh, ped, seed)
        self.source = "synthetic"
        self.flows = {g: FlowEstimator(now) for g in self.layout.veh_groups()}
        self.cam_ok = {c.id: True for c in self.layout.cameras}
        self.forced: Mode | None = None
        self.trip: str | None = None
        self.signals = dict(self.ctrl.signals)
        self.obs = Observation()
        self.minute = MinuteStats()
        self.events: list[SiteEvent] = []
        self.recent: list[dict] = []
        self.now = now

    # ---------- управление из центра ----------
    def set_camera(self, cam_id: str, ok: bool) -> None:
        if cam_id not in self.cam_ok or self.cam_ok[cam_id] == ok:
            return
        self.cam_ok[cam_id] = ok
        title = next(c.title for c in self.layout.cameras if c.id == cam_id)
        self._event("info" if ok else "warn", "camera", f"{title}: {'сигнал восстановлен' if ok else 'нет изображения'}")

    def set_forced_mode(self, mode: Mode | None) -> None:
        self.forced = mode
        self._event("warn" if mode else "info", "operator",
                    f"Оператор: {'режим «' + MODE_TITLES[mode] + '»' if mode else 'автоматический выбор режима'}")

    def reset_trip(self) -> None:
        if self.trip:
            self._event("info", "safety", "Оператор сбросил аварию защиты")
        self.trip = None

    def update_params(self, p: Params) -> None:
        self.p = p
        self.ctrl.p = p
        self.safety.p = p
        self._event("info", "params", "Параметры алгоритма изменены")

    def demo_group(self, size: int = 10) -> None:
        g = self.world.add_group(self.now, size)
        self._event("info", "demo", f"Демонстрация: группа {size} чел. у перехода «{self.layout.groups[g].title.lower()}»")

    def demo_emergency(self) -> None:
        g = self.world.add_emergency()
        self._event("warn", "emergency", f"Спецтранспорт приближается: {self.layout.groups[g].title.lower()}")

    # ---------- такт ----------
    def covered(self) -> set[str]:
        out: set[str] = set()
        for c in self.layout.cameras:
            if self.cam_ok[c.id]:
                out |= set(c.covers)
        return out

    def _observe(self, truth: Observation) -> Observation:
        vis = self.covered()
        o = Observation(cameras=dict(self.cam_ok))
        for g in self.layout.ped_groups():
            ok = g in vis
            o.waiting[g] = truth.waiting.get(g) if ok else None
            o.max_wait[g] = truth.max_wait.get(g) if ok else None
            o.on_crosswalk[g] = truth.on_crosswalk.get(g) if ok else None
        for g in self.layout.veh_groups():
            ok = g in vis
            o.queue[g] = truth.queue.get(g) if ok else None
            o.eta[g] = truth.eta.get(g) if ok else None
            o.flow_vph[g] = self.flows[g].estimate(self.now, self.p) if ok else None
            if ok and g in truth.emergency:
                o.emergency.add(g)
        return o

    def _choose_mode(self) -> tuple[Mode, str]:
        if self.trip:
            return Mode.FLASHING, f"сработала защита: {self.trip}"
        if self.forced:
            return self.forced, "задан оператором"
        bad = [c.title for c in self.layout.cameras if not self.cam_ok[c.id]]
        if len(bad) == len(self.layout.cameras):
            return Mode.FIXED, "нет изображения ни с одной камеры"
        if bad:
            return Mode.DEGRADED, "нет изображения: " + ", ".join(bad)
        return Mode.ADAPTIVE, "все камеры исправны"

    def tick(self, now: float, dt: float, wall: datetime | None = None) -> None:
        self.now = now
        wall = wall or datetime.now()
        hour = wall.hour + wall.minute / 60
        m = self.world.step(now, dt, hour, self.signals)
        for g, n in m.passed.items():
            self.flows[g].add(now, n)
        self.obs = self._observe(self.world.observe(now))

        mode, reason = self._choose_mode()
        self.ctrl.set_mode(mode, reason, now)
        before = dict(self.signals)
        sig = self.ctrl.tick(now, self.obs)
        errs = self.safety.check(now, sig)
        if errs:
            self.trip = errs[0]
            self._event("critical", "safety", "Защита: " + "; ".join(errs))
            self.ctrl.set_mode(Mode.FLASHING, f"сработала защита: {errs[0]}", now)
            sig = dict(self.ctrl.signals)
            self.safety.check(now, sig)
        self.signals = sig

        st = self.minute
        st.mode_s[self.ctrl.mode.value] = st.mode_s.get(self.ctrl.mode.value, 0.0) + dt
        st.veh_passed += sum(m.passed.values())
        st.veh_stopped += sum(m.stopped.values())
        st.ped_arrived += sum(m.arrived.values())
        for g, wait, red in m.served:
            st.ped_served += 1
            st.wait_sum += wait
            st.wait_max = max(st.wait_max, wait)
            if red:
                st.violations += 1
        for g in self.layout.ped_groups():
            if before.get(g) != Ped.GREEN and sig.get(g) == Ped.GREEN:
                st.ped_phases += 1
                n = self.obs.waiting.get(g) or 0
                if n >= self.p.group_threshold:
                    st.groups += 1
                    self._event("info", "group", f"Группа {n} чел.: «{self.layout.groups[g].title.lower()}»")
        for e in self.ctrl.drain_events():
            self._event(e.level, e.kind, e.message)

    def take_minute(self) -> MinuteStats:
        st, self.minute = self.minute, MinuteStats()
        return st

    def drain_events(self) -> list[SiteEvent]:
        ev, self.events = self.events, []
        return ev

    def _event(self, level: str, kind: str, message: str) -> None:
        e = SiteEvent(datetime.now(), level, kind, message)
        self.events.append(e)
        self.recent.insert(0, {"ts": e.ts.isoformat(timespec="seconds"), "level": level, "kind": kind, "message": message})
        del self.recent[40:]

    # ---------- состояние для админки ----------
    def status(self) -> str:
        m = self.ctrl.mode
        if m == Mode.FLASHING:
            return "alarm"
        if m in (Mode.DEGRADED, Mode.FIXED):
            return "warn"
        return "ok"

    def summary(self) -> dict:
        o = self.obs
        return {
            "id": self.id, "status": self.status(), "mode": self.ctrl.mode.value,
            "mode_title": MODE_TITLES[self.ctrl.mode], "source": self.source,
            "stage": self.layout.stages[self.ctrl.stage].title if self.ctrl.trans is None else "Смена фазы",
            "waiting": sum(v or 0 for v in o.waiting.values()),
            "flow_vph": round(sum(v or 0 for v in o.flow_vph.values())),
            "cameras_ok": sum(self.cam_ok.values()), "cameras_total": len(self.cam_ok),
        }

    def snapshot(self) -> dict:
        o = self.obs
        c = self.ctrl
        st = self.layout.stages[c.stage]
        return {
            **self.summary(),
            "ts": time.time(),
            "mode_reason": c.mode_reason,
            "stage_id": st.id if c.trans is None else None,
            "next_stage": self.layout.stages[c.trans.to].title if c.trans else None,
            "stage_time_s": round(self.now - (c.trans.t0 if c.trans else c.stage_t0), 1),
            "status_text": c.status,
            "info": c.info,
            "signals": {g: (s.value if hasattr(s, "value") else s) for g, s in self.signals.items()},
            "observation": {
                "waiting": o.waiting, "max_wait": {k: None if v is None else round(v, 1) for k, v in o.max_wait.items()},
                "on_crosswalk": o.on_crosswalk, "queue": o.queue,
                "eta": {k: None if v is None else round(v, 1) for k, v in o.eta.items()},
                "flow_vph": {k: None if v is None else round(v) for k, v in o.flow_vph.items()},
                "flow_window_s": {g: round(f.window_s) for g, f in self.flows.items()},
                "emergency": sorted(o.emergency),
            },
            "cameras": [{"id": x.id, "title": x.title, "ok": self.cam_ok[x.id], "covers": list(x.covers)}
                        for x in self.layout.cameras],
            "trip": self.trip,
            "forced": self.forced.value if self.forced else None,
            "events": self.recent[:15],
        }

    def layout_json(self) -> dict:
        L = self.layout
        return {
            "kind": L.kind,
            "groups": [{"id": g.id, "kind": g.kind, "title": g.title, "length_m": g.length_m} for g in L.groups.values()],
            "stages": [{"id": s.id, "title": s.title, "veh": list(s.veh), "ped": list(s.ped)} for s in L.stages],
            "conflicts": [sorted(p) for p in L.conflicts],
        }


__all__ = ["SiteRuntime", "MinuteStats", "SiteEvent", "Veh", "Ped", "Mode"]
