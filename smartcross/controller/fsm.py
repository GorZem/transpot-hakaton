"""Traffic light controller for a signalized pedestrian crossing.

Pure logic, no I/O: the caller feeds `tick(now, observation)` and reads `state`.
Time is injected, which makes the controller deterministic and testable
(the simulator runs it faster than real time).

Phase sequence (Russian practice, ГОСТ Р 52289):
    VEH_GREEN -> VEH_GREEN_BLINK -> VEH_YELLOW -> ALL_RED_TO_PED
    -> PED_GREEN -> PED_GREEN_BLINK -> ALL_RED_TO_VEH -> VEH_GREEN
plus FLASHING (жёлтый мигающий, пешеходные секции выключены).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from smartcross.config import Config
from smartcross.controller import timing


class Phase(str, Enum):
    VEH_GREEN = "veh_green"
    VEH_GREEN_BLINK = "veh_green_blink"
    VEH_YELLOW = "veh_yellow"
    ALL_RED_TO_PED = "all_red_to_ped"
    PED_GREEN = "ped_green"
    PED_GREEN_BLINK = "ped_green_blink"
    ALL_RED_TO_VEH = "all_red_to_veh"
    FLASHING = "flashing_yellow"


class Mode(str, Enum):
    ADAPTIVE = "adaptive"      # all cameras OK
    DEGRADED = "degraded"      # part of the cameras lost: adaptive + guaranteed recall
    FIXED = "fixed"            # no vision: fixed cycle
    FLASHING = "flashing"      # yellow flashing (night / failure, by config)


class VehSignal(str, Enum):
    GREEN = "green"
    GREEN_BLINK = "green_blink"
    YELLOW = "yellow"
    RED = "red"
    YELLOW_BLINK = "yellow_blink"


class PedSignal(str, Enum):
    RED = "red"
    GREEN = "green"
    GREEN_BLINK = "green_blink"
    OFF = "off"


SIGNALS: dict[Phase, tuple[VehSignal, PedSignal]] = {
    Phase.VEH_GREEN: (VehSignal.GREEN, PedSignal.RED),
    Phase.VEH_GREEN_BLINK: (VehSignal.GREEN_BLINK, PedSignal.RED),
    Phase.VEH_YELLOW: (VehSignal.YELLOW, PedSignal.RED),
    Phase.ALL_RED_TO_PED: (VehSignal.RED, PedSignal.RED),
    Phase.PED_GREEN: (VehSignal.RED, PedSignal.GREEN),
    Phase.PED_GREEN_BLINK: (VehSignal.RED, PedSignal.GREEN_BLINK),
    Phase.ALL_RED_TO_VEH: (VehSignal.RED, PedSignal.RED),
    Phase.FLASHING: (VehSignal.YELLOW_BLINK, PedSignal.OFF),
}

# Allowed transitions. Anything else is a controller bug; the conflict monitor checks it.
TRANSITIONS: dict[Phase, set[Phase]] = {
    Phase.VEH_GREEN: {Phase.VEH_GREEN_BLINK, Phase.FLASHING},
    Phase.VEH_GREEN_BLINK: {Phase.VEH_YELLOW},
    Phase.VEH_YELLOW: {Phase.ALL_RED_TO_PED},
    Phase.ALL_RED_TO_PED: {Phase.PED_GREEN},
    Phase.PED_GREEN: {Phase.PED_GREEN_BLINK},
    Phase.PED_GREEN_BLINK: {Phase.ALL_RED_TO_VEH},
    Phase.ALL_RED_TO_VEH: {Phase.VEH_GREEN},
    Phase.FLASHING: {Phase.ALL_RED_TO_VEH},
}


@dataclass
class Observation:
    """Fused sensor picture for one control tick."""
    ped_waiting: dict[str, int] = field(default_factory=dict)  # side -> count in waiting zone
    ped_on_crossing: int = 0
    veh_in_approach: int = 0          # vehicles currently inside approach zones
    veh_flow_vph: float = 0.0         # smoothed flow, veh/h, all approaches
    emergency: bool = False           # emergency vehicle approaching
    button: bool = False              # physical call button (optional hardware)
    ped_vision_ok: bool = True        # all pedestrian zones are covered by healthy cameras
    veh_vision_ok: bool = True        # all approach zones are covered by healthy cameras

    @property
    def ped_total(self) -> int:
        return sum(self.ped_waiting.values())


@dataclass
class Event:
    t: float
    kind: str               # "phase", "mode", "ped_served"
    data: dict


@dataclass
class State:
    phase: Phase
    phase_start: float
    mode: Mode
    reason: str = ""

    @property
    def veh(self) -> VehSignal:
        return SIGNALS[self.phase][0]

    @property
    def ped(self) -> PedSignal:
        return SIGNALS[self.phase][1]


class Controller:
    def __init__(self, cfg: Config, now: float = 0.0, mode: Mode = Mode.ADAPTIVE):
        self.cfg = cfg
        # Safe start: all red, then vehicle green.
        self.state = State(Phase.ALL_RED_TO_VEH, now, mode, "запуск")
        self.requested_mode = mode
        self.demand_since: float | None = None     # pedestrian call latched at
        self.ped_seen_since: float | None = None   # for debounce
        self.max_group = 0                         # biggest group seen during the current call
        self.acc_delay = 0.0                       # accumulated pedestrian delay of the current call, person·s
        self._last_tick = now
        self.last_veh_seen = now                   # for gap-out
        self.emergency_since: float | None = None
        self.last_ped_phase_end = now
        self.fixed_veh_green_s: float | None = None  # engine may override with historical timing
        self.planned_ped_green = timing.ped_green_s(cfg)
        self.events: list[Event] = []
        self.now = now

    # ------------------------------------------------------------------ API
    def set_mode(self, mode: Mode, reason: str = "") -> None:
        if mode != self.requested_mode:
            self.requested_mode = mode
            self._emit("mode_request", mode=mode.value, reason=reason)

    def drain_events(self) -> list[Event]:
        ev, self.events = self.events, []
        return ev

    def time_in_phase(self) -> float:
        return self.now - self.state.phase_start

    def ped_wait(self) -> float:
        return 0.0 if self.demand_since is None else self.now - self.demand_since

    def estimated_wait(self, obs: Observation) -> float | None:
        """Expected total wait of the current call (for the operator UI)."""
        if self.demand_since is None:
            return None
        wait = self.now - self.demand_since
        n = max(obs.ped_total, 1)
        k = timing.switch_threshold(self.cfg, obs.veh_flow_vph, max(self.max_group, obs.ped_total))
        eta = wait
        acc = self.acc_delay
        while acc < k and eta < self.cfg.timing.max_ped_wait_s:  # integrate forward, 0.5 s steps
            acc += timing.ped_cost_rate(self.cfg, n, eta) * 0.5
            eta += 0.5
        green_left = timing.capacity_green_s(self.cfg, obs.veh_flow_vph) - self.time_in_phase()
        eta = max(eta, wait + max(green_left, 0))
        return min(eta, self.cfg.timing.max_ped_wait_s)

    def tick(self, now: float, obs: Observation) -> State:
        self.now = now
        dt = max(now - self._last_tick, 0.0)
        self._last_tick = now
        self._update_inputs(obs, dt)
        if self.state.mode != self.requested_mode and self._mode_switch_safe():
            self._apply_mode()
        handler = getattr(self, "_on_" + self.state.phase.name.lower())
        handler(obs)
        return self.state

    # ------------------------------------------------------------ internals
    def _emit(self, kind: str, **data) -> None:
        self.events.append(Event(self.now, kind, data))

    def _go(self, phase: Phase, reason: str = "") -> None:
        assert phase in TRANSITIONS[self.state.phase], f"illegal {self.state.phase} -> {phase}"
        prev = self.state.phase
        self._emit("phase", prev=prev.value, phase=phase.value,
                   duration=round(self.time_in_phase(), 2), reason=reason, mode=self.state.mode.value)
        self.state = State(phase, self.now, self.state.mode, reason)

    def _mode_switch_safe(self) -> bool:
        # Logic modes (adaptive/degraded/fixed) switch any time; entering flashing only
        # from vehicle green (pedestrians already have red), leaving it goes via all-red.
        if self.requested_mode == Mode.FLASHING:
            return self.state.phase == Phase.VEH_GREEN
        return True

    def _apply_mode(self) -> None:
        old = self.state.mode
        self.state.mode = self.requested_mode
        self._emit("mode", prev=old.value, mode=self.state.mode.value)
        if self.state.mode == Mode.FLASHING:
            self._go(Phase.FLASHING, "режим мигания")
        elif self.state.phase == Phase.FLASHING:
            self._go(Phase.ALL_RED_TO_VEH, "выход из мигания")

    def _update_inputs(self, obs: Observation, dt: float) -> None:
        now = self.now
        if obs.veh_in_approach > 0 or not obs.veh_vision_ok:
            self.last_veh_seen = now
        if obs.emergency:
            if self.emergency_since is None:
                self.emergency_since = now
        else:
            self.emergency_since = None

        # Pedestrian demand is latched only while vehicles have right of way.
        if self.state.phase in (Phase.VEH_GREEN, Phase.FLASHING):
            if obs.ped_total > 0:
                if self.ped_seen_since is None:
                    self.ped_seen_since = now
                if self.demand_since is None and now - self.ped_seen_since >= self.cfg.timing.demand_debounce_s:
                    self.demand_since = self.ped_seen_since
                    w0 = now - self.ped_seen_since  # they've already waited
                    self.acc_delay = timing.ped_cost_rate(self.cfg, obs.ped_total, w0 / 2) * w0
            else:
                self.ped_seen_since = None
            if obs.button and self.demand_since is None:
                self.demand_since = now
            if self.demand_since is not None:
                self.max_group = max(self.max_group, obs.ped_total)
                self.acc_delay += timing.ped_cost_rate(self.cfg, max(obs.ped_total, 1), now - self.demand_since) * dt

    # ---- phase handlers
    def _on_veh_green(self, obs: Observation) -> None:
        t = self.cfg.timing
        green = self.time_in_phase()
        mode = self.state.mode

        if mode == Mode.FIXED:
            veh_green = self.fixed_veh_green_s or self.cfg.fallback.fixed.veh_green_s
            if green >= veh_green:
                self._start_ped_cycle("фиксированный цикл")
            return

        # adaptive / degraded
        if mode == Mode.DEGRADED and self.now - self.last_ped_phase_end >= self.cfg.control.degraded_recall_s \
                and green >= t.veh_min_green_s:
            self._start_ped_cycle("деградированный режим: гарантированный вызов")
            return
        if self.demand_since is None:
            return

        wait = self.now - self.demand_since
        group = max(self.max_group, obs.ped_total)
        flow = obs.veh_flow_vph

        if self.emergency_since is not None and self.now - self.emergency_since < self.cfg.control.emergency_hold_max_s:
            return  # hold green for the emergency vehicle

        # "Empty road": nobody in sight AND on average less than one vehicle is expected
        # to arrive during the pedestrian phase — then serving pedestrians costs ~nothing.
        expected_veh = flow / 3600.0 * timing.ped_phase_total_s(self.cfg)
        road_empty = (obs.veh_vision_ok and obs.veh_in_approach == 0 and expected_veh < 1.0
                      and self.now - self.last_veh_seen >= t.gap_out_s)
        if road_empty and green >= t.veh_min_green_empty_s and wait >= t.min_ped_delay_empty_s:
            self._start_ped_cycle("нет ТС")
            return
        if green < t.veh_min_green_s:
            return
        if wait >= t.max_ped_wait_s:
            self._start_ped_cycle(f"предел ожидания {t.max_ped_wait_s:.0f} c")
            return
        if green < timing.capacity_green_s(self.cfg, flow):
            return  # vehicles still need this green to keep up with the flow
        k = timing.switch_threshold(self.cfg, flow, group)
        if self.acc_delay >= k:
            who = f"группа {group} чел." if group >= self.cfg.control.group_threshold else f"{group} чел."
            self._start_ped_cycle(f"накопленное ожидание {self.acc_delay:.0f} ≥ {k:.0f} чел·с ({who})")
            return
        # gap-out: in a gap of the vehicle stream nobody has to brake right now, so switching
        # is cheaper — but vehicles arriving during the pedestrian phase still pay, hence a factor.
        if obs.veh_vision_ok and self.now - self.last_veh_seen >= t.gap_out_s and self.acc_delay >= k * t.gap_out_factor:
            self._start_ped_cycle("разрыв в потоке ТС")

    def _start_ped_cycle(self, reason: str) -> None:
        self._go(Phase.VEH_GREEN_BLINK, reason)

    def _on_veh_green_blink(self, obs: Observation) -> None:
        if self.time_in_phase() >= self.cfg.timing.veh_green_blink_s:
            self._go(Phase.VEH_YELLOW)

    def _on_veh_yellow(self, obs: Observation) -> None:
        if self.time_in_phase() >= self.cfg.timing.veh_yellow_s:
            self._go(Phase.ALL_RED_TO_PED)

    def _on_all_red_to_ped(self, obs: Observation) -> None:
        if self.time_in_phase() >= self.cfg.timing.all_red_s:
            group = max(self.max_group, obs.ped_total)
            self.planned_ped_green = timing.ped_green_s(self.cfg, group)
            self._emit("ped_served", wait=round(self.ped_wait(), 2), group=group,
                       called=self.demand_since is not None, mode=self.state.mode.value)
            self.demand_since = None
            self.ped_seen_since = None
            self.max_group = 0
            self.acc_delay = 0.0
            self._go(Phase.PED_GREEN, f"зелёный пешеходам {self.planned_ped_green:.0f} c")

    def _on_ped_green(self, obs: Observation) -> None:
        if self.time_in_phase() >= self.planned_ped_green:
            self._go(Phase.PED_GREEN_BLINK)

    def _on_ped_green_blink(self, obs: Observation) -> None:
        if self.time_in_phase() >= self.cfg.timing.ped_blink_s:
            self._go(Phase.ALL_RED_TO_VEH)

    def _on_all_red_to_veh(self, obs: Observation) -> None:
        t = self.cfg.timing
        base = t.ped_clearance_s
        if self.time_in_phase() < base:
            return
        # Someone is still on the crossing: keep all-red a bit longer (vision-based safety).
        if obs.ped_vision_ok and obs.ped_on_crossing > 0 and self.time_in_phase() < base + t.ped_clear_extension_max_s:
            return
        self.last_ped_phase_end = self.now
        self._go(Phase.VEH_GREEN)

    def _on_flashing(self, obs: Observation) -> None:
        pass  # leaves only through a mode change (see _apply_mode)
