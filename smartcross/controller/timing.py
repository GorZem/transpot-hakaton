"""Signal timing calculations.

References:
  * Webster F.V. "Traffic signal settings", 1958 — optimal cycle C0 = (1.5 L + 5) / (1 - Y).
  * ГОСТ Р 52289-2019 / ОДМ 218.6.003-2011 — пешеходная фаза должна обеспечить
    переход проезжей части со скоростью ~1.3 м/с (1.0 м/с для маломобильных).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from smartcross.config import Config


@dataclass(frozen=True)
class PedPhasePlan:
    green_s: float
    blink_s: float
    clearance_s: float


def ped_green_s(cfg: Config, group_size: int = 0) -> float:
    """Pedestrian green (steady) duration.

    Pedestrians who start at the very end of the steady green must be able to finish
    during the blinking green + clearance all-red, so steady green covers the start-up
    time plus the walk time minus the blinking part.
    """
    t = cfg.timing
    walk = cfg.site.crossing_length_m / cfg.site.ped_speed_mps
    base = max(t.ped_min_green_s, t.ped_start_s + walk - t.ped_blink_s)
    extra = 0.0
    if group_size > cfg.control.group_threshold:
        extra = min(t.ped_group_extra_max_s,
                    (group_size - cfg.control.group_threshold) * t.ped_group_extra_per_person_s)
    return base + extra


def ped_phase_total_s(cfg: Config, group_size: int = 0) -> float:
    """Time vehicles lose to one pedestrian phase including intergreens."""
    t = cfg.timing
    return (t.veh_green_blink_s + t.veh_yellow_s + t.all_red_s
            + ped_green_s(cfg, group_size) + t.ped_blink_s + t.ped_clearance_s)


def webster_cycle_s(cfg: Config, flow_vph_total: float) -> float:
    """Optimal cycle for a two-phase (vehicles / pedestrians) crossing.

    For vehicles the whole pedestrian phase is lost time L. Critical flow ratio
    y = q_lane / s, where q_lane is the average flow per lane.
    """
    c = cfg.control
    lost = ped_phase_total_s(cfg)
    q_lane = max(flow_vph_total, 0.0) / cfg.site.lanes_total
    y = min(q_lane / c.saturation_flow_vphpl, 0.95)
    if y >= 0.9:
        return c.cycle_max_s
    cycle = (1.5 * lost + 5) / (1 - y)
    return min(max(cycle, c.cycle_min_s, lost + cfg.timing.veh_min_green_s), c.cycle_max_s)


def flow_ratio(cfg: Config, flow_vph_total: float) -> float:
    """y = q / s per lane (average over lanes)."""
    q_lane = max(flow_vph_total, 0.0) / cfg.site.lanes_total
    return q_lane / cfg.control.saturation_flow_vphpl


def capacity_green_s(cfg: Config, flow_vph_total: float) -> float:
    """Minimum vehicle green that keeps the degree of saturation at or below x_max.

    With cycle C = g + P (P = pedestrian phase incl. intergreens) the degree of saturation
    is x = y*C/g. Solving x = x_max for g:   g = y*P / (x_max - y).
    A shorter green can't serve the vehicle flow: queues would grow without bound.
    """
    y = flow_ratio(cfg, flow_vph_total)
    x = cfg.control.max_saturation
    if y >= x:
        return cfg.timing.max_ped_wait_s
    return max(cfg.timing.veh_min_green_s, y * ped_phase_total_s(cfg) / (x - y))


def ped_phase_cost(cfg: Config, flow_vph_total: float) -> float:
    """Delay (person·s) a pedestrian phase causes to vehicle occupants.

    Vehicles arriving during the red R = P queue up and then discharge at saturation flow;
    the total delay of such a red interval is q·R² / (2(1 − y)) (uniform-delay term of
    Webster's formula for one cycle).
    """
    q = max(flow_vph_total, 0.0) / 3600.0
    y = min(flow_ratio(cfg, flow_vph_total), 0.95)
    r = ped_phase_total_s(cfg)
    return cfg.control.veh_occupancy * q * r * r / (2 * (1 - y))


def switch_threshold(cfg: Config, flow_vph_total: float, group_size: int) -> float:
    """Accumulated pedestrian delay (person·s, weighted) at which the pedestrian phase pays off.

    Serving pedestrians costs vehicles a roughly fixed K per phase; waiting pedestrians
    accumulate delay at a rate equal to their number. Switching once the weighted
    accumulated pedestrian delay reaches K is the classic optimal clearing rule
    (EOQ: cycle T* = sqrt(2K/λ) for arrival rate λ). Groups accumulate delay faster, so
    they are served sooner automatically; group_delay_factor adds explicit priority.
    """
    k = ped_phase_cost(cfg, flow_vph_total) / cfg.control.ped_time_weight
    if group_size >= cfg.control.group_threshold:
        k *= cfg.control.group_delay_factor
    return k


def ped_cost_rate(cfg: Config, n: int, wait: float) -> float:
    """Growth rate of the pedestrian waiting cost: each second of waiting costs more than the
    previous one (people get impatient and start crossing on red — HCM: >30 s is critical)."""
    tau = cfg.control.ped_impatience_s
    return n * (1 + wait / tau) if tau > 0 else float(n)


def expected_ped_delay_s(cfg: Config, flow_vph_total: float, group_size: int) -> float:
    """Wait of a pedestrian (group of `group_size`) arriving alone at a steady vehicle green:
    for display and planning (the controller itself integrates the real accumulated cost).
    Solves  n·(w + w²/(2τ)) = K  for w."""
    n = max(group_size, 1)
    k = switch_threshold(cfg, flow_vph_total, n)
    tau = cfg.control.ped_impatience_s
    t = tau * (-1 + math.sqrt(1 + 2 * k / (n * tau))) if tau > 0 else k / n
    return min(max(t, capacity_green_s(cfg, flow_vph_total)), cfg.timing.max_ped_wait_s)


# kept for the fixed / historical mode and for reference in the docs
def target_ped_delay_s(cfg: Config, flow_vph_total: float, group_size: int) -> float:
    return expected_ped_delay_s(cfg, flow_vph_total, group_size)
