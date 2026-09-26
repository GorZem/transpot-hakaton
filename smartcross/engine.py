"""Runtime engine: cameras -> fusion -> controller -> output, plus health, modes and statistics."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from smartcross.config import CameraConfig, Config, ConfigStore
from smartcross.controller import timing
from smartcross.controller.fsm import Controller, Mode, Observation, Phase
from smartcross.controller.safety import ConflictMonitor
from smartcross.hw.output import MockOutput, lamps, make_output
from smartcross.stats.db import StatsDB
from smartcross.traffic.flow import FlowEstimator
from smartcross.vision.camera import FAULTS, CameraWorker, FrameResult

log = logging.getLogger(__name__)

TICK_S = 0.1


@dataclass
class CamState:
    status: str = "starting"   # ok / lost / frozen / dark / error / disabled / starting
    healthy: bool = False      # stable, with recovery hysteresis
    ok_since: float | None = None
    last: FrameResult | None = None


def in_time_window(now: datetime, start: str, end: str) -> bool:
    def mins(s: str) -> int:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    cur, a, b = now.hour * 60 + now.minute, mins(start), mins(end)
    return a <= cur < b if a <= b else cur >= a or cur < b


def default_detector_factory(cfg: Config) -> Callable[[], Callable]:
    def make():
        from smartcross.vision.detector import YoloDetector
        return YoloDetector(cfg.detector)
    return make


class Engine:
    def __init__(self, store: ConfigStore, db: StatsDB | None = None,
                 detector_factory: Callable[[Config], Callable[[], Callable]] = default_detector_factory,
                 clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.cfg = store.cfg
        self.db = db
        self.detector_factory = detector_factory
        self.clock = clock
        now = clock()
        self.controller = Controller(self.cfg, now)
        self.monitor = ConflictMonitor(self.cfg)
        self.output = make_output(self.cfg.output)
        self.flow = FlowEstimator(self.cfg.flow_window, start=now)
        self.results: queue.Queue[FrameResult] = queue.Queue(maxsize=500)
        self.workers: dict[str, CameraWorker] = {}
        self.cams: dict[str, CamState] = {}
        self.forced_mode: Mode | None = None
        self.conflict_latched: str | None = None
        self.manual_call = False
        self.snapshot: dict = {}
        self._cfg_version = store.version
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_flush = now
        self._hist_profile: dict[int, float] = {}
        self._hist_loaded = 0.0
        self._last_mode_reason = ""
        self._last_flow = None
        self._lock = threading.Lock()
        self._failed: set[str] = set()
        self._coverage_bad_since: float | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._sync_cameras()
        self._thread = threading.Thread(target=self._run, name="control", daemon=True)
        self._thread.start()
        self._event("info", "system", "Система запущена")

    def stop(self) -> None:
        self._stop.set()
        for w in self.workers.values():
            w.stop()
        if self._thread:
            self._thread.join(timeout=2)
        if self.db:
            self.db.flush(force_minute=True)
        self.output.close()

    def _run(self) -> None:
        next_t = self.clock()
        while not self._stop.is_set():
            try:
                self.tick(self.clock())
            except Exception:  # noqa: BLE001
                log.exception("control tick failed")
            next_t += TICK_S
            self._stop.wait(max(0.0, next_t - self.clock()))

    # ------------------------------------------------------------------ cameras
    def _sync_cameras(self) -> None:
        wanted = {c.id: c for c in self.cfg.cameras if c.enabled}
        for cid in list(self.workers):
            w = self.workers[cid]
            c = wanted.get(cid)
            if c is None or (c.source, c.fps, c.loop) != (w.cam.source, w.cam.fps, w.cam.loop):
                w.stop()
                del self.workers[cid]
            elif c.zones != w.cam.zones:
                w.set_zones(c.zones)
        for cid, c in wanted.items():
            if cid not in self.workers:
                w = CameraWorker(c, self.cfg.fallback, self.detector_factory(self.cfg), self.results, self.clock)
                self.workers[cid] = w
                w.start()
        self.cams = {cid: self.cams.get(cid, CamState()) for cid in [c.id for c in self.cfg.cameras]}

    def inject_fault(self, camera_id: str, fault: str | None) -> None:
        if fault is not None and fault not in FAULTS:
            raise ValueError(f"unknown fault {fault}")
        w = self.workers.get(camera_id)
        if w is None:
            raise KeyError(camera_id)
        w.fault = fault
        self._event("warn" if fault else "info", "fault_injection",
                    f"Камера {camera_id}: {'имитация отказа ' + fault if fault else 'имитация отказа снята'}")

    def _update_health(self, now: float) -> None:
        fb = self.cfg.fallback
        for cam in self.cfg.cameras:
            st = self.cams.setdefault(cam.id, CamState())
            w = self.workers.get(cam.id)
            if not cam.enabled or w is None:
                status = "disabled"
            elif w.health.error and now - w.health.last_frame_t > fb.camera_timeout_s:
                status = "error"
            elif now - w.health.last_frame_t > fb.camera_timeout_s:
                status = "lost" if w.health.frames else "starting"
            elif w.health.frozen_since is not None and now - w.health.frozen_since > fb.frozen_s:
                status = "frozen"
            elif w.health.dark:
                status = "dark"
            elif w.health.frames == 0:
                status = "starting"
            else:
                status = "ok"
            prev_healthy = st.healthy
            if status == "ok":
                if st.ok_since is None:
                    st.ok_since = now
                # first start doesn't need the recovery delay, a recovery after a failure does
                healthy = prev_healthy or cam.id not in self._failed or now - st.ok_since >= fb.recovery_s
            else:
                st.ok_since = None
                healthy = False
            if prev_healthy and not healthy:
                self._failed.add(cam.id)
                self._event("error", "camera", f"Камера {cam.id}: отказ ({status})")
            elif healthy and not prev_healthy and cam.id in self._failed:
                self._event("info", "camera", f"Камера {cam.id}: восстановлена")
            st.status, st.healthy = status, healthy

    # ------------------------------------------------------------------ fusion
    def _fuse(self, now: float) -> tuple[Observation, dict]:
        latest: dict[str, FrameResult] = {}
        passed_dir: dict[str, int] = {}
        passed_cls: dict[str, int] = {}
        ped_new = 0
        drained: list[FrameResult] = []
        while True:
            try:
                drained.append(self.results.get_nowait())
            except queue.Empty:
                break
        cams = {c.id: c for c in self.cfg.cameras}
        # which camera is the counting source for each direction: the first healthy one covering it
        dir_owner: dict[str, str] = {}
        for c in self.cfg.cameras:
            if not self.cams.get(c.id, CamState()).healthy:
                continue
            for z in c.zones:
                if z.type in ("approach", "count_line"):
                    dir_owner.setdefault(z.direction or z.id, c.id)
        crosswalk_owner = next((c.id for c in self.cfg.cameras if self.cams.get(c.id, CamState()).healthy
                                and any(z.type == "crosswalk" for z in c.zones)), None)
        for r in drained:
            if r.camera_id not in cams:
                continue
            self.cams[r.camera_id].last = r
            if not self.cams[r.camera_id].healthy:
                continue
            for d, n in r.zones.veh_passed.items():
                if dir_owner.get(d) == r.camera_id:
                    passed_dir[d] = passed_dir.get(d, 0) + n
                    self.flow.add_vehicle(r.t, n)
            for k, n in r.zones.veh_passed_by_class.items():
                passed_cls[k] = passed_cls.get(k, 0) + n
            if r.camera_id == crosswalk_owner:
                ped_new += r.zones.ped_new

        for cid, st in self.cams.items():
            if st.healthy and st.last and now - st.last.t < self.cfg.fallback.camera_timeout_s:
                latest[cid] = st.last

        # coverage: every zone key must be seen by at least one healthy camera
        ped_keys, veh_keys = set(), set()
        for c in self.cfg.cameras:
            for z in c.zones:
                if z.type == "ped_wait":
                    ped_keys.add(z.side or z.id)
                elif z.type == "crosswalk":
                    ped_keys.add("__crosswalk")
                elif z.type == "approach":
                    veh_keys.add(z.direction or z.id)
        ped_cov, veh_cov = set(), set()
        waiting: dict[str, int] = {}
        on_cross = 0
        in_appr: dict[str, int] = {}
        appr_len: dict[str, float] = {}
        emergency = False
        for cid, r in latest.items():
            zr = r.zones
            for side, n in zr.ped_waiting.items():
                waiting[side] = max(waiting.get(side, 0), n)
                ped_cov.add(side)
            if any(z.type == "crosswalk" for z in cams[cid].zones):
                ped_cov.add("__crosswalk")
                on_cross = max(on_cross, zr.ped_on_crossing)
            for d, n in zr.veh_in_approach.items():
                in_appr[d] = max(in_appr.get(d, 0), n)
                appr_len[d] = max(appr_len.get(d, 0), zr.approach_length_m.get(d, 0))
                veh_cov.add(d)
            emergency |= zr.emergency

        ped_ok = bool(ped_keys) and ped_keys <= ped_cov
        veh_ok = bool(veh_keys) and veh_keys <= veh_cov
        n_appr = sum(in_appr.values())
        fs = self.flow.update(now, n_appr, sum(appr_len.values()))
        self._last_flow = fs
        flow = fs.flow_vph
        if not veh_ok:
            flow = self._historical_flow(now) if self._historical_flow(now) is not None else flow

        button = self.output.read_button() or self.manual_call
        self.manual_call = False
        obs = Observation(ped_waiting=waiting, ped_on_crossing=on_cross, veh_in_approach=n_appr,
                          veh_flow_vph=flow, emergency=emergency, button=button,
                          ped_vision_ok=ped_ok, veh_vision_ok=veh_ok)
        extra = dict(passed_dir=passed_dir, passed_cls=passed_cls, ped_new=ped_new, flow=fs)
        return obs, extra

    def _historical_flow(self, now: float) -> float | None:
        if self.db is None:
            return None
        if now - self._hist_loaded > 600 or not self._hist_loaded:
            try:
                self._hist_profile = self.db.hourly_flow_profile()
            except Exception:  # noqa: BLE001
                self._hist_profile = {}
            self._hist_loaded = now
        return self._hist_profile.get(datetime.now().hour)

    # ------------------------------------------------------------------ modes
    def _choose_mode(self, obs: Observation) -> tuple[Mode, str]:
        if self.conflict_latched:
            return Mode.FLASHING, f"конфликт-монитор: {self.conflict_latched}"
        if self.forced_mode is not None:
            return self.forced_mode, "ручной режим оператора"
        n_cfg = [c for c in self.cfg.cameras if c.enabled]
        healthy = [c for c in n_cfg if self.cams.get(c.id, CamState()).healthy]
        night = self.cfg.night
        if night.enabled and night.mode == "flashing" and in_time_window(datetime.now(), night.start, night.end):
            return Mode.FLASHING, "ночной режим"
        starting = [c for c in n_cfg if self.cams.get(c.id, CamState()).status == "starting"]
        if starting and not self._failed:
            return Mode.FIXED, "запуск камер"
        if not n_cfg or not healthy:
            mode = Mode.FLASHING if self.cfg.fallback.all_lost_mode == "flashing" else Mode.FIXED
            return mode, "нет исправных камер" if n_cfg else "камеры не настроены"
        lost = [c.id for c in n_cfg if c not in healthy]
        if lost:
            return Mode.DEGRADED, f"потеряны камеры: {', '.join(lost)}"
        # a coverage gap must persist a bit: a healthy camera may just not have sent its first result yet
        if not (obs.ped_vision_ok and obs.veh_vision_ok):
            if self._coverage_bad_since is None:
                self._coverage_bad_since = self.clock()
            if self.clock() - self._coverage_bad_since >= 2.0:
                return Mode.DEGRADED, "неполное покрытие зон"
            return self.controller.requested_mode, self._last_mode_reason
        self._coverage_bad_since = None
        return Mode.ADAPTIVE, "все камеры исправны"

    # ------------------------------------------------------------------ main tick
    def tick(self, now: float) -> None:
        if self.store.version != self._cfg_version:
            self._apply_config()
        self._update_health(now)
        obs, extra = self._fuse(now)
        mode, reason = self._choose_mode(obs)
        if (mode, reason) != (self.controller.requested_mode, self._last_mode_reason):
            if mode != self.controller.requested_mode:
                self._event("warn" if mode != Mode.ADAPTIVE else "info", "mode",
                            f"Режим → {mode.value}: {reason}")
            self._last_mode_reason = reason
        self.controller.set_mode(mode, reason)
        if mode == Mode.FIXED and not obs.veh_vision_ok:
            hist = self._historical_flow(now)
            if hist is not None:  # fixed cycle tuned to the historical flow of this hour
                self.controller.fixed_veh_green_s = timing.webster_cycle_s(self.cfg, hist) - timing.ped_phase_total_s(self.cfg)
        state = self.controller.tick(now, obs)
        violations = self.monitor.check(now, state.phase)
        if violations and not self.conflict_latched:
            self.conflict_latched = violations[0]
            self._event("error", "conflict", "; ".join(violations))
        self.output.set(state.veh, state.ped)
        self._record(now, obs, extra)
        self._publish(now, obs, extra)

    def _apply_config(self) -> None:
        self._cfg_version = self.store.version
        self.cfg = self.store.cfg
        self.controller.cfg = self.cfg
        self.monitor.cfg = self.cfg
        self.flow.cfg = self.cfg.flow_window
        self._sync_cameras()
        self._event("info", "config", "Конфигурация обновлена")

    # ------------------------------------------------------------------ stats
    def _event(self, level: str, kind: str, message: str) -> None:
        log.info("[%s] %s", kind, message)
        if self.db:
            self.db.add_event(datetime.now(), level, kind, message)

    def _record(self, now: float, obs: Observation, extra: dict) -> None:
        evs = self.controller.drain_events()
        if self.db is None:
            return
        wall = datetime.now()
        for e in evs:
            if e.kind == "phase":
                self.db.add_phase(wall, e.data["prev"], e.data["duration"], e.data["phase"], e.data["mode"],
                                  e.data["reason"])
            elif e.kind == "ped_served":
                self.db.add_ped_service(wall, e.data["wait"], e.data["group"], e.data["called"], e.data["mode"])
        fs = extra["flow"]
        self.db.add_traffic(wall, extra["passed_dir"], extra["passed_cls"], extra["ped_new"], obs.ped_total,
                            fs.flow_vph, fs.density_vpkm)
        if now - self._last_flush > 5:
            self.db.flush()
            self._last_flush = now

    def _publish(self, now: float, obs: Observation, extra: dict) -> None:
        c = self.controller
        st = c.state
        fs = extra["flow"]
        remaining = None
        if st.phase == Phase.PED_GREEN:
            remaining = c.planned_ped_green - c.time_in_phase() + self.cfg.timing.ped_blink_s
        elif st.phase == Phase.PED_GREEN_BLINK:
            remaining = self.cfg.timing.ped_blink_s - c.time_in_phase()
        target = c.estimated_wait(obs) if st.phase == Phase.VEH_GREEN else None
        cams = []
        for cam in self.cfg.cameras:
            s = self.cams.get(cam.id, CamState())
            w = self.workers.get(cam.id)
            cams.append({
                "id": cam.id, "name": cam.name, "status": s.status, "healthy": s.healthy,
                "fps": w.health.fps if w else 0, "infer_ms": w.health.infer_ms if w else 0,
                "fault": w.fault if w else None, "error": w.health.error if w else "",
                "persons": s.last.zones.persons_total if s.last else 0,
                "vehicles": s.last.zones.vehicles_total if s.last else 0,
            })
        snap = {
            "phase": st.phase.value, "veh": st.veh.value, "ped": st.ped.value, "mode": st.mode.value,
            "mode_reason": self._last_mode_reason, "reason": st.reason,
            "time_in_phase": round(c.time_in_phase(), 1), "ped_remaining": None if remaining is None else round(remaining, 1),
            "ped_wait": round(c.ped_wait(), 1), "target_delay": None if target is None else round(target, 1),
            "demand": c.demand_since is not None,
            "lamps": lamps(st.veh, st.ped, now),
            "obs": {
                "ped_waiting": obs.ped_waiting, "ped_total": obs.ped_total, "ped_on_crossing": obs.ped_on_crossing,
                "veh_in_approach": obs.veh_in_approach, "veh_flow_vph": round(obs.veh_flow_vph, 1),
                "emergency": obs.emergency, "ped_vision_ok": obs.ped_vision_ok, "veh_vision_ok": obs.veh_vision_ok,
            },
            "flow": {"flow_vph": fs.flow_vph, "raw_flow_vph": fs.raw_flow_vph, "window_s": fs.window_s,
                     "count_in_window": fs.count_in_window, "density_vpkm": fs.density_vpkm},
            "cycle_s": round(timing.webster_cycle_s(self.cfg, obs.veh_flow_vph), 1),
            "capacity_green_s": round(timing.capacity_green_s(self.cfg, obs.veh_flow_vph), 1),
            "ped_phase_cost": round(timing.ped_phase_cost(self.cfg, obs.veh_flow_vph)),
            "acc_delay": round(c.acc_delay),
            "cameras": cams, "conflict": self.conflict_latched, "forced_mode": self.forced_mode.value if self.forced_mode else None,
            "output": "mock" if isinstance(self.output, MockOutput) else "gpio",
        }
        with self._lock:
            self.snapshot = snap

    def get_snapshot(self) -> dict:
        with self._lock:
            return dict(self.snapshot)

    # ------------------------------------------------------------------ operator actions
    def set_forced_mode(self, mode: Mode | None) -> None:
        self.forced_mode = mode
        self._event("warn" if mode else "info", "operator",
                    f"Оператор: {'режим ' + mode.value if mode else 'автоматический режим'}")

    def press_button(self) -> None:
        self.manual_call = True

    def reset_conflict(self) -> None:
        if self.conflict_latched:
            self._event("info", "operator", "Оператор сбросил конфликт-монитор")
        self.conflict_latched = None
        self.monitor = ConflictMonitor(self.cfg)
