"""Узел объекта: камеры → детектор → восприятие → режим → контроллер → защита → контроллер светофора.

Один экземпляр на оснащённый объект. Может работать внутри центра (режим «центр») или отдельным
процессом на встраиваемом ПК у светофора (режим «узел»): интерфейс одинаковый — tick() и snapshot().
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from node.control import Controller
from node.equipment import CameraPtzLink, EquipmentLink
from node.flow import FlowEstimator
from node.model import MODE_TITLES, Camera, Mode, Observation, Ped, Veh, build_layout
from node.params import Params
from node.perception import Perception, Update
from node.safety import Safety
from node.vision.camera import FAULT_TITLES, CameraStream
from node.vision.geometry import CameraModel, reproject_zones

# Состояния камеры, при которых изображение есть, но для распознавания непригодно (в админке — оранжевым).
WARN_TITLES = {"moving": "поворот камеры: зоны пересчитываются", "cv": "компьютерное зрение не работает",
               "empty": "не видно ни людей, ни машин"}
WARN_FAULTS = {"black", "blind"}   # признаки по изображению, которые тоже «оранжевые»: камера цела, но не видит
PTZ_SETTLE_S = 1.5                 # после остановки поворота выждать, затем пересчитать зоны
CV_STALE_S = 6.0                   # столько без результатов распознавания — компьютерное зрение не работает


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
        self.cam_pose: dict[str, tuple[float, float]] = {c.id: (0.0, 0.0) for c in self.cam_models}  # поворот, по которому считаются зоны
        self.perception = Perception(site, self.layout, self.cam_models, self.p)
        cover = self.perception.coverage
        self.layout.cameras = [Camera(c.id, c.title, tuple(sorted(cover.get(c.id, ())))) for c in self.layout.cameras]
        self.ctrl = Controller(self.layout, self.p, now)
        self.safety = Safety(self.layout, self.p)
        self.flows = {g: FlowEstimator(now) for g in self.layout.veh_groups()}
        self.streams = {c["id"]: CameraStream(c["id"], (equipment_url or "").rstrip("/") + c["stream"])
                        for c in site.get("cameras", [])}
        self.ptz = CameraPtzLink(equipment_url, {c["id"]: c.get("ptz") or f"/api/cameras/{c['id']}/ptz"
                                                 for c in site.get("cameras", [])}) if equipment_url else None
        self.cam_warn: dict[str, str | None] = {cid: None for cid in self.streams}
        self._motion: dict[str, float | None] = {cid: None for cid in self.streams}  # когда замечен поворот
        self._last_seen: dict[str, float] = {}     # камера -> когда последний раз видела человека или машину
        self._last_result: dict[str, float] = {}   # камера -> когда пришёл последний результат распознавания
        self.worker = None
        self.on_zones_changed = None               # центр сохраняет разметку и положения камер
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
        self.worker = detector_worker
        for cid, st in self.streams.items():
            st.start()
            detector_worker.add(cid, self._source(cid), lambda fr, dets, cid=cid: self._on_detections(cid, fr, dets))
        if self.link:
            self.link.start()
        if self.ptz:
            self.ptz.start()

    def stop(self) -> None:
        for st in self.streams.values():
            st.stop()
        if self.link:
            self.link.stop()
        if self.ptz:
            self.ptz.stop()

    def _on_detections(self, cid: str, fr, dets) -> None:  # поток детектора
        mono = time.monotonic()
        self._last_result[cid] = mono
        if dets:
            self._last_seen[cid] = mono
        self.perception.push(cid, fr, dets)

    def _source(self, cid: str):
        st = self.streams[cid]
        return lambda: st.latest() if self.cam_fault.get(cid) is None else None

    # ---------- управление оператора ----------
    released = False  # оператор вернул объект на штатную программу контроллера
    auto_static: str | None = None  # статический режим включён автоматически: почему (камера непригодна)

    def static_reason(self) -> str | None:
        """Статический режим включается сам, если хоть одна камера объекта непригодна: неисправна,
        не видит (туман, закрыта, пустая сцена), поворачивается или не работает распознавание.
        По одной камере система не управляет: половина подходов и переходов вслепую."""
        bad = [f"{c.title} — {self.cam_state(c.id)[2]}" for c in self.layout.cameras if self.cam_state(c.id)[0] is not None]
        return "; ".join(bad) if bad else None

    def set_forced_mode(self, mode: Mode | None) -> None:
        self.forced = mode
        self.released = False
        self._event("warn" if mode else "info", "operator",
                    f"Оператор: {'режим «' + MODE_TITLES[mode] + '»' if mode else 'автоматический выбор режима'}")

    def set_released(self) -> None:
        """Стандартный (статический) режим: система отпускает светофор, дорожный контроллер работает
        по своей штатной программе. Камеры и статистика продолжают работать, фазы система не переключает."""
        self.forced = None
        self.released = True
        self._event("warn", "operator", "Оператор: статический режим — штатная программа контроллера, система не управляет светофором")

    def reset_trip(self) -> None:
        if self.trip:
            self._event("info", "safety", "Оператор сбросил аварию защиты")
        self.trip = None

    def update_params(self, p: Params) -> None:
        self.p = self.ctrl.p = self.safety.p = self.perception.p = p
        self._event("info", "params", "Параметры алгоритма изменены")

    # ---------- зоны на кадрах камер ----------
    custom_zones: dict[str, list[dict]] | None = None

    def zones_for(self, cam_id: str) -> tuple[list[dict], bool]:
        """Зоны камеры (координаты 0…1) и признак «нарисованы вручную»."""
        if self.custom_zones and cam_id in self.custom_zones:
            return self.custom_zones[cam_id], True
        cam = next(c for c in self.cam_models if c.id == cam_id)
        return self.perception.geo.auto_zones(cam), False

    def restore(self, saved: dict | None) -> None:
        """Разметка из базы: зоны и положения камер, при которых они нарисованы."""
        saved = dict(saved or {})
        poses = saved.pop("_poses", {}) or {}
        for cid, (pan, tilt) in poses.items():
            if cid in self.cam_pose and (pan or tilt):
                self._set_model(cid, pan, tilt)
        self.set_zones(saved or None, log=False)

    def zones_record(self) -> dict:
        """Разметка для сохранения: ручные зоны и положения камер."""
        return {**(self.custom_zones or {}), "_poses": {cid: list(p) for cid, p in self.cam_pose.items()}}

    def _set_model(self, cid: str, pan: float, tilt: float) -> tuple[CameraModel, CameraModel]:
        i = next(k for k, c in enumerate(self.cam_models) if c.id == cid)
        old = self.cam_models[i]
        new = old.turned(pan, tilt)
        self.cam_models[i] = new
        self.cam_pose[cid] = (pan, tilt)
        self.perception.cams[cid] = new
        self.perception.coverage = cover = self.perception.geo.coverage(self.cam_models)
        self.layout.cameras = [Camera(c.id, c.title, tuple(sorted(cover.get(c.id, ())))) for c in self.layout.cameras]
        return old, new

    def apply_pose(self, cid: str, pan: float, tilt: float) -> None:
        """Камера повернулась: пересчитать калибровку, покрытие и зоны (ручные — через плоскость земли)."""
        old, new = self._set_model(cid, pan, tilt)
        custom = dict(self.custom_zones or {})
        if cid in custom:
            moved = reproject_zones(custom[cid], old, new)
            if moved:
                custom[cid] = moved
            else:
                del custom[cid]
        self.set_zones(custom or None, log=False)
        title = next((c.title for c in self.layout.cameras if c.id == cid), cid)
        self._event("info", "camera", f"{title}: повёрнута на {pan:+.0f}° по азимуту, {tilt:+.0f}° по наклону "
                    f"от положения при монтаже — калибровка и зоны пересчитаны")
        if self.on_zones_changed:
            self.on_zones_changed()

    def ptz_command(self, cid: str, pan: float | None = None, tilt: float | None = None,
                    relative: bool = False, home: bool = False) -> dict:
        if self.ptz is None or cid not in self.streams:
            raise KeyError(cid)
        snap = self.ptz.command(cid, pan, tilt, relative, home)
        self._motion[cid] = time.monotonic()   # объект уходит в фиксированный план сразу, не дожидаясь опроса
        return snap

    def set_zones(self, custom: dict[str, list[dict]] | None, log: bool = True) -> None:
        """Применить разметку. Камеры без ручной разметки получают автоматическую в том же виде,
        чтобы зона, видимая двумя камерами, проверялась одинаково."""
        self.custom_zones = custom or None
        geo = self.perception.geo
        if self.custom_zones:
            geo.set_image_zones(self.cam_models, {c.id: self.zones_for(c.id)[0] for c in self.cam_models})
        else:
            geo.set_image_zones(self.cam_models, None)
        if log:
            self._event("info", "zones", "Разметка зон камер изменена" if custom else "Разметка зон сброшена к автоматической")

    # ---------- такт ----------
    def healthy(self) -> set[str]:
        """Камеры, по которым можно распознавать: исправны и изображение пригодно."""
        return {cid for cid, f in self.cam_fault.items() if f is None and self.cam_warn.get(cid) is None}

    def cam_state(self, cid: str) -> tuple[str | None, str, str]:
        """(код, уровень ok | warn | fault, подпись). warn — оранжевый: камера цела, но распознавать не по чему."""
        f = self.cam_fault.get(cid)
        if f is not None:
            return f, ("warn" if f in WARN_FAULTS else "fault"), FAULT_TITLES[f]
        w = self.cam_warn.get(cid)
        if w is not None:
            title = WARN_TITLES[w]
            if w == "empty":
                title += f" дольше {self.p.empty_scene_s:.0f} с"
            return w, "warn", title
        return None, "ok", FAULT_TITLES[None]

    def _check_ptz(self, mono: float) -> None:
        if self.ptz is None:
            return
        for cid in self.streams:
            st = self.ptz.state.get(cid)
            if not st:
                continue
            dev = (float(st.get("pan_deg", 0.0)), float(st.get("tilt_deg", 0.0)))
            cur = self.cam_pose[cid]
            changed = abs(dev[0] - cur[0]) > 0.05 or abs(dev[1] - cur[1]) > 0.05
            if st.get("moving"):
                self._motion[cid] = mono
            elif changed and self._motion[cid] is None:
                self._motion[cid] = mono                      # повернули между опросами
            elif self._motion[cid] is not None and mono - self._motion[cid] >= PTZ_SETTLE_S:
                if changed:
                    self.apply_pose(cid, *dev)
                self._motion[cid] = None
                self._last_seen[cid] = mono                   # новому виду — полный срок до «пустой сцены»

    def _check_warn(self, mono: float) -> None:
        titles = {c.id: c.title for c in self.layout.cameras}
        w = self.worker
        cv_down = w is not None and (bool(w.error) or not w.ready.is_set() or getattr(w, "paused", False)
                                     or mono - getattr(w, "last_cycle", mono) > CV_STALE_S)
        for cid in self.streams:
            ok_img = self.cam_fault.get(cid) is None
            if not ok_img:
                self._last_seen[cid] = mono                   # после восстановления — полный срок
                self._last_result[cid] = mono
            warn = None
            if self._motion.get(cid) is not None:
                warn = "moving"
            elif ok_img and w is not None and (cv_down or mono - self._last_result.setdefault(cid, mono) > CV_STALE_S):
                warn = "cv"
            elif ok_img and w is not None and mono - self._last_seen.setdefault(cid, mono) > self.p.empty_scene_s:
                warn = "empty"
            if warn != self.cam_warn.get(cid):
                prev = self.cam_warn.get(cid)
                self.cam_warn[cid] = warn
                if warn is not None:
                    self._event("warn", "camera", f"{titles[cid]}: {self.cam_state(cid)[2]}")
                elif prev in ("empty", "cv") and ok_img:
                    self._event("info", "camera", f"{titles[cid]}: изображение снова пригодно для распознавания")

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
        cams = self.layout.cameras
        moving = [c.title for c in cams if self.cam_warn.get(c.id) == "moving"]
        if moving:
            return Mode.FIXED, "поворот камеры: " + ", ".join(moving)
        if cams and all(self.cam_warn.get(c.id) == "cv" for c in cams):
            return Mode.FIXED, "компьютерное зрение не работает"
        bad = [f"{c.title} — {self.cam_state(c.id)[2]}" for c in cams if self.cam_state(c.id)[0] is not None]
        if bad and len(bad) == len(cams):
            return Mode.FIXED, "нет пригодного изображения ни с одной камеры: " + "; ".join(bad)
        if bad:
            return Mode.DEGRADED, "непригодна: " + "; ".join(bad)
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

    WARMUP_S = 8.0  # после запуска камеры подключаются: отказы и смены режима в журнал не пишем

    def tick(self, now: float, dt: float, wall: datetime | None = None) -> None:
        self.now = now
        if self._t_start is None:
            self._t_start = time.monotonic()
        warm = time.monotonic() - self._t_start < self.WARMUP_S
        mono = time.monotonic()
        self._check_ptz(mono)
        if warm:
            for cid, st in self.streams.items():
                self.cam_fault[cid] = st.health()
                self._last_seen[cid] = self._last_result[cid] = mono
            self.ctrl.set_mode(*self._choose_mode(), now)
            self.ctrl.drain_events()
            self.status_text = "Запуск: подключение камер и контроллера объекта"
            return
        self._check_cameras()
        self._check_warn(mono)
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
        auto = self.static_reason() if (self.forced is None and not self.trip and not self.released) else None
        if auto != self.auto_static:
            if auto and not self.auto_static:
                self._event("critical", "static", f"Статический режим: штатная программа контроллера — {auto}")
            elif not auto:
                self._event("info", "static", "Камеры снова пригодны: система принимает управление светофором")
            self.auto_static = auto
        if self.released or auto:
            # управление отдано контроллеру объекта; без команд и heartbeat связь сама вернёт его к своей программе
            self.engaged = False
            if link:
                link.active = False
            self.signals = dict(link.device_groups) if link else {}
            self.status_text = ("Статический режим: штатная программа контроллера, включён оператором" if self.released
                                else f"Статический режим (автоматически): {auto}")
            self.minute.mode_s["local"] = self.minute.mode_s.get("local", 0.0) + dt
            self.ctrl.drain_events()
            return
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
    _t_start: float | None = None
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
        if self.trip or (self.link and not self.link.connected) or self.ctrl.mode == Mode.FLASHING or self.auto_static:
            return "alarm"
        if self.released:
            return "warn"
        if not self.engaged or self.ctrl.mode in (Mode.DEGRADED, Mode.FIXED):
            return "warn"
        return "ok"

    def mode_title(self) -> str:
        if self.released:
            return "Статический режим (оператор)"
        if self.auto_static:
            return "Статический режим: камера непригодна"
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
            "static": "operator" if self.released else ("auto" if self.auto_static else None),
            "static_reason": self.auto_static,
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
            "cameras": [self._cam_json(x) for x in self.layout.cameras],
            "equipment": {"connected": bool(link and link.connected), "mode": link.device_mode if link else None,
                          "error": link.last_error if link else "не настроен", "rejected": link.rejected if link else None},
            "trip": self.trip,
            "forced": "local" if self.released else (self.forced.value if self.forced else None),
            "events": self.recent[:15],
        }

    def ptz_json(self, cid: str) -> dict | None:
        st = self.ptz.state.get(cid) if self.ptz else None
        if not st:
            return None
        cam = next(c for c in self.cam_models if c.id == cid)
        return {"pan_deg": st.get("pan_deg", 0.0), "tilt_deg": st.get("tilt_deg", 0.0), "goal": st.get("goal"),
                "moving": self._motion.get(cid) is not None, "limits": st.get("limits"),
                "azimuth_deg": round(cam.azimuth_deg, 1), "tilt_down_deg": round(cam.tilt_down_deg, 1),
                "applied": {"pan_deg": self.cam_pose[cid][0], "tilt_deg": self.cam_pose[cid][1]}}

    def _cam_json(self, x) -> dict:
        code, level, title = self.cam_state(x.id)
        return {"id": x.id, "title": x.title, "ok": level == "ok", "level": level, "state": code,
                "fault": code, "fault_title": title,
                "fps": round(self.streams[x.id].fps, 1), "covers": list(x.covers), "ptz": self.ptz_json(x.id)}

    def layout_json(self) -> dict:
        L = self.layout
        return {
            "kind": L.kind,
            "groups": [{"id": g.id, "kind": g.kind, "title": g.title, "length_m": g.length_m} for g in L.groups.values()],
            "stages": [{"id": s.id, "title": s.title, "veh": list(s.veh), "ped": list(s.ped)} for s in L.stages],
            "conflicts": [sorted(p) for p in L.conflicts],
        }


__all__ = ["SiteRuntime", "MinuteStats", "SiteEvent", "Veh", "Ped", "Mode"]
