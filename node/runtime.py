"""Узел объекта: камеры → детектор → восприятие → режим → контроллер → защита → контроллер светофора.

Один экземпляр на оснащённый объект. Может работать внутри центра (режим «центр») или отдельным
процессом на встраиваемом ПК у светофора (режим «узел»): интерфейс одинаковый — tick() и snapshot().
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from node.control import Controller
from node.equipment import EquipmentLink
from node.flow import FlowEstimator
from node.model import MODE_TITLES, Camera, Mode, Observation, Ped, Veh, build_layout
from node.params import Params
from node.perception import Perception, Update
from node.safety import Safety
from node.vision.camera import FAULT_TITLES, CameraStream
from node.vision.geometry import CameraModel


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


class SiteRuntime:
    def __init__(self, site: dict, params: Params | None, equipment_url: str | None, now: float = 0.0):
        self.site = site
        self.id = site["id"]
        self.p = params or Params()
        self.layout = build_layout(site)
        self.cam_models = [CameraModel(c) for c in site.get("cameras", [])]
        self.perception = Perception(site, self.layout, self.cam_models, self.p)
        cover = self.perception.coverage
        self.layout.cameras = [Camera(c.id, c.title, tuple(sorted(cover.get(c.id, ())))) for c in self.layout.cameras]
        self.ctrl = Controller(self.layout, self.p, now)
        self.safety = Safety(self.layout, self.p)
        self.flows = {g: FlowEstimator(now) for g in self.layout.veh_groups()}
        self.streams = {c["id"]: CameraStream(c["id"], (equipment_url or "").rstrip("/") + c["stream"])
                        for c in site.get("cameras", [])}
        ctl = site.get("controller") or {}
        self.link = EquipmentLink(equipment_url, ctl["path"]) if equipment_url and ctl.get("path") else None
        self.engaged = False          # система управляет контроллером объекта
        self.forced: Mode | None = None
        self.trip: str | None = None
        self.signals: dict[str, str] = {}
        self.obs = Observation()
        self.cam_fault: dict[str, str | None] = {cid: "offline" for cid in self.streams}
        self.minute = MinuteStats()
        self.events: list[SiteEvent] = []
        self.recent: list[dict] = []
        self.now = now
        self.status_text = "Подключение к оборудованию объекта"

    # ---------- запуск ----------
    def start(self, detector_worker) -> None:
        for cid, st in self.streams.items():
            st.start()
            detector_worker.add(cid, self._source(cid), lambda fr, dets, cid=cid: self.perception.push(cid, fr, dets))
        if self.link:
            self.link.start()

    def stop(self) -> None:
        for st in self.streams.values():
            st.stop()
        if self.link:
            self.link.stop()

    def _source(self, cid: str):
        st = self.streams[cid]
        return lambda: st.latest() if self.cam_fault.get(cid) is None else None

    # ---------- управление оператора ----------
    def set_forced_mode(self, mode: Mode | None) -> None:
        self.forced = mode
        self._event("warn" if mode else "info", "operator",
                    f"Оператор: {'режим «' + MODE_TITLES[mode] + '»' if mode else 'автоматический выбор режима'}")

    def reset_trip(self) -> None:
        if self.trip:
            self._event("info", "safety", "Оператор сбросил аварию защиты")
        self.trip = None

    def update_params(self, p: Params) -> None:
        self.p = self.ctrl.p = self.safety.p = self.perception.p = p
        self._event("info", "params", "Параметры алгоритма изменены")

    # ---------- такт ----------
    def healthy(self) -> set[str]:
        return {cid for cid, f in self.cam_fault.items() if f is None}

    def _check_cameras(self) -> None:
        titles = {c.id: c.title for c in self.layout.cameras}
        for cid, st in self.streams.items():
            f = st.health()
            if f != self.cam_fault[cid]:
                prev = self.cam_fault[cid]
                self.cam_fault[cid] = f
                if f is None:
                    self._event("info", "camera", f"{titles[cid]}: исправна")
                elif prev is None or f != "offline":
                    self._event("warn", "camera", f"{titles[cid]}: {FAULT_TITLES[f]}")

    def _choose_mode(self) -> tuple[Mode, str]:
        if self.trip:
            return Mode.FLASHING, f"сработала защита: {self.trip}"
        if self.forced:
            return self.forced, "задан оператором"
        bad = [c.title for c in self.layout.cameras if self.cam_fault.get(c.id) is not None]
        if bad and len(bad) == len(self.layout.cameras):
            return Mode.FIXED, "нет изображения ни с одной камеры"
        if bad:
            return Mode.DEGRADED, "неисправна: " + ", ".join(bad)
        return Mode.ADAPTIVE, "все камеры исправны"

    def _steady_stage(self) -> tuple[int, set[str]] | None:
        """Устойчивая фаза на контроллере объекта: зелёный одному направлению, остальным красный."""
        g = self.link.device_groups if self.link else {}
        if not g:
            return None
        for i, st in enumerate(self.layout.stages):
            veh_ok = all(g.get(v) == "green" for v in st.veh) and all(
                g.get(v) == "red" for v in self.layout.veh_groups() if v not in st.veh)
            peds = {p for p in self.layout.ped_groups() if g.get(p) == "green"}
            ped_ok = all(g.get(p) == "red" for p in self.layout.ped_groups() if p not in st.ped) and \
                all(g.get(p) in ("green", "red") for p in st.ped)
            if st.ped_only:
                veh_ok = all(g.get(v) == "red" for v in self.layout.veh_groups())
                ped_ok = ped_ok and bool(peds)
            if veh_ok and ped_ok:
                return i, peds
        return None

    def tick(self, now: float, dt: float, wall: datetime | None = None) -> None:
        self.now = now
        self._check_cameras()
        healthy = self.healthy()
        up = self.perception.update(healthy, self.signals)
        if up is not None:
            self.obs = up.obs
            self._account(up, now)
        elif time.time() - self.perception.last_ts > 1.5:
            self.obs = self.perception.refresh(time.time(), healthy)
        for g, f in self.flows.items():
            if self.obs.queue.get(g) is not None:
                self.obs.flow_vph[g] = f.estimate(now, self.p)

        mode, reason = self._choose_mode()
        self.ctrl.set_mode(mode, reason, now)
        link = self.link
        if link is None or not link.connected:
            if self.engaged:
                self._event("critical", "equipment", "Нет связи с контроллером объекта: объект работает по своей программе")
            self.engaged = False
            self.status_text = "Нет связи с контроллером объекта"
        elif link.device_mode == "flash" and self.engaged:
            self.engaged = False
            self._event("critical", "equipment", "Контроллер объекта перешёл в жёлтый мигающий (своя защита)")
        elif link.device_mode == "local" and self.engaged and link.rejected is None and time.monotonic() - self._engaged_at > 3:
            self.engaged = False
            self._event("warn", "equipment", "Контроллер объекта вернулся к своей программе: управление будет принято заново")
        if not self.engaged and link and link.connected:
            if self.ctrl.mode == Mode.FLASHING:
                self.engaged = True
                self._engaged_at = time.monotonic()
            else:
                steady = self._steady_stage()
                if steady is None:
                    self.status_text = "Ожидание устойчивой фазы для подхвата управления"
                else:
                    self.safety = Safety(self.layout, self.p)
                    self.ctrl.adopt(steady[0], steady[1], now)
                    self.engaged = True
                    self._engaged_at = time.monotonic()
        if link:
            link.active = self.engaged
            if link.rejected and link.rejected != self._last_rejected:
                self._event("critical", "equipment", f"Контроллер объекта отклонил команду: {link.rejected}")
            self._last_rejected = link.rejected
        if self.engaged:
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
            link.send(sig)
            self.status_text = self.ctrl.status
            for g in self.layout.ped_groups():
                if before.get(g) != Ped.GREEN and sig.get(g) == Ped.GREEN:
                    self.minute.ped_phases += 1
                    n = self.obs.waiting.get(g) or 0
                    if n >= self.p.group_threshold:
                        self.minute.groups += 1
                        self._event("info", "group", f"Группа {n} чел.: «{self.layout.groups[g].title.lower()}»")
        else:
            self.signals = dict(link.device_groups) if link else {}
        st = self.minute
        key = self.ctrl.mode.value if self.engaged else "local"
        st.mode_s[key] = st.mode_s.get(key, 0.0) + dt
        for e in self.ctrl.drain_events():
            self._event(e.level, e.kind, e.message)

    _engaged_at = 0.0
    _last_rejected: str | None = None

    def _account(self, up: Update, now: float) -> None:
        st = self.minute
        for g, n in up.passed.items():
            self.flows[g].add(now, n)
            st.veh_passed += n
        st.veh_stopped += sum(up.stopped.values())
        st.ped_arrived += sum(up.arrived.values())
        for g, wait, red in up.served:
            st.ped_served += 1
            st.wait_sum += wait
            st.wait_max = max(st.wait_max, wait)
            if red:
                st.violations += 1

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
        if self.trip or (self.link and not self.link.connected) or self.ctrl.mode == Mode.FLASHING:
            return "alarm"
        if not self.engaged or self.ctrl.mode in (Mode.DEGRADED, Mode.FIXED):
            return "warn"
        return "ok"

    def mode_title(self) -> str:
        if self.link is None or not self.link.connected:
            return "Нет связи с контроллером"
        if not self.engaged:
            return "Своя программа контроллера"
        return MODE_TITLES[self.ctrl.mode]

    def summary(self) -> dict:
        o = self.obs
        c = self.ctrl
        return {
            "id": self.id, "equipped": True, "status": self.status(), "mode": c.mode.value if self.engaged else "local",
            "mode_title": self.mode_title(),
            "stage": (self.layout.stages[c.stage].title if c.trans is None else "Смена фазы") if self.engaged else "—",
            "waiting": sum(v or 0 for v in o.waiting.values()),
            "flow_vph": round(sum(v or 0 for v in o.flow_vph.values())),
            "cameras_ok": len(self.healthy()), "cameras_total": len(self.streams),
        }

    def snapshot(self) -> dict:
        o = self.obs
        c = self.ctrl
        st = self.layout.stages[c.stage]
        link = self.link
        return {
            **self.summary(),
            "ts": time.time(),
            "engaged": self.engaged,
            "mode_reason": c.mode_reason,
            "stage_id": st.id if (c.trans is None and self.engaged) else None,
            "next_stage": self.layout.stages[c.trans.to].title if (c.trans and self.engaged) else None,
            "stage_time_s": round(self.now - (c.trans.t0 if c.trans else c.stage_t0), 1) if self.engaged else None,
            "status_text": self.status_text,
            "info": c.info if self.engaged else {},
            "signals": {g: str(getattr(s, "value", s)) for g, s in self.signals.items()},
            "observation": {
                "waiting": o.waiting, "max_wait": {k: None if v is None else round(v, 1) for k, v in o.max_wait.items()},
                "on_crosswalk": o.on_crosswalk, "queue": o.queue,
                "eta": {k: None if v is None else round(v, 1) for k, v in o.eta.items()},
                "flow_vph": {k: None if v is None else round(v) for k, v in o.flow_vph.items()},
                "flow_window_s": {g: round(f.window_s) for g, f in self.flows.items()},
                "emergency": sorted(o.emergency),
            },
            "cameras": [{"id": x.id, "title": x.title, "ok": self.cam_fault.get(x.id) is None,
                         "fault": self.cam_fault.get(x.id), "fault_title": FAULT_TITLES[self.cam_fault.get(x.id)],
                         "fps": round(self.streams[x.id].fps, 1), "covers": list(x.covers)}
                        for x in self.layout.cameras],
            "equipment": {"connected": bool(link and link.connected), "mode": link.device_mode if link else None,
                          "error": link.last_error if link else "не настроен", "rejected": link.rejected if link else None},
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
