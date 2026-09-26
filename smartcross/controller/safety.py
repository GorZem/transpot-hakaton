"""Conflict monitor (аналог MMU/конфликт-монитора дорожного контроллера).

Independent from the controller logic: watches the output stream and reports
violations. The engine forces yellow flashing if the monitor trips.
"""
from __future__ import annotations

from smartcross.config import Config
from smartcross.controller.fsm import SIGNALS, TRANSITIONS, Phase, PedSignal, VehSignal

VEH_MOVING = {VehSignal.GREEN, VehSignal.GREEN_BLINK, VehSignal.YELLOW}
PED_MOVING = {PedSignal.GREEN, PedSignal.GREEN_BLINK}


def min_duration(cfg: Config, phase: Phase) -> float:
    t = cfg.timing
    return {
        Phase.VEH_GREEN: 0.0,  # may be short when the road is empty; bounded by veh_min_green_empty_s
        Phase.VEH_GREEN_BLINK: t.veh_green_blink_s,
        Phase.VEH_YELLOW: t.veh_yellow_s,
        Phase.ALL_RED_TO_PED: t.all_red_s,
        Phase.PED_GREEN: t.ped_min_green_s,
        Phase.PED_GREEN_BLINK: t.ped_blink_s,
        Phase.ALL_RED_TO_VEH: t.ped_clearance_s,
        Phase.FLASHING: 0.0,
    }[phase]


class ConflictMonitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.phase: Phase | None = None
        self.since: float | None = None  # None: the first observed phase, its real start is unknown
        self.violations: list[str] = []

    def check(self, now: float, phase: Phase) -> list[str]:
        found: list[str] = []
        veh, ped = SIGNALS[phase]
        if veh in VEH_MOVING and ped in PED_MOVING:
            found.append(f"конфликт сигналов: ТС={veh.value}, пешеходы={ped.value}")
        if self.phase is not None and phase != self.phase:
            if phase not in TRANSITIONS[self.phase]:
                found.append(f"недопустимый переход {self.phase.value} -> {phase.value}")
            dur = None if self.since is None else now - self.since
            need = min_duration(self.cfg, self.phase)
            if dur is not None and dur + 1e-6 < need:
                found.append(f"фаза {self.phase.value} длилась {dur:.2f} c < {need:.2f} c")
        if phase != self.phase:
            self.since = None if self.phase is None else now
            self.phase = phase
        self.violations.extend(found)
        return found
