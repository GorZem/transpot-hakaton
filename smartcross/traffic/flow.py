"""Traffic flow / density estimation.

Flow q (авт/ч) is estimated from vehicle passage events (a tracked vehicle crossing a
count line, or leaving an approach zone). The averaging interval is chosen adaptively:

    T = clamp(N_target / q_prev, T_min, T_max)

The count in a window is ~Poisson, so its relative error is 1/sqrt(N). Asking for
N_target ≈ 30 vehicles gives ≈18% error — accurate enough for timing, while the window
stays short (≈1-2 min) in dense traffic and follows changes quickly. In sparse traffic
(night) the window grows to T_max (15 min) instead of jumping on every single car.
On top of that an EWMA smooths successive estimates.

Density k (авт/км) = vehicles currently in the approach zone / zone length.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from smartcross.config import FlowWindowConfig


@dataclass
class FlowSnapshot:
    flow_vph: float          # smoothed flow, veh/h
    raw_flow_vph: float      # flow in the current window
    window_s: float          # chosen window length
    count_in_window: int
    density_vpkm: float      # vehicles per km (approach zones)
    in_approach: int


class FlowEstimator:
    def __init__(self, cfg: FlowWindowConfig, start: float = 0.0):
        self.cfg = cfg
        self.events: deque[float] = deque()
        self.start = start
        self.flow = 0.0
        self.window = cfg.max_s
        self._last_update = start

    def add_vehicle(self, t: float, n: int = 1) -> None:
        for _ in range(n):
            self.events.append(t)

    def _count_since(self, t0: float) -> int:
        return sum(1 for e in self.events if e >= t0)

    def update(self, now: float, in_approach: int = 0, approach_length_m: float = 0.0) -> FlowSnapshot:
        c = self.cfg
        # drop events older than the largest window
        while self.events and self.events[0] < now - c.max_s:
            self.events.popleft()
        # adaptive window from the previous estimate
        if self.flow > 0:
            self.window = min(max(c.target_count / (self.flow / 3600.0), c.min_s), c.max_s)
        else:
            self.window = c.max_s
        # we can't look further back than we've been observing
        eff_window = max(min(self.window, now - self.start), 1e-6)
        n = self._count_since(now - eff_window)
        # right after start the history is short: normalize by at least min_s to avoid wild estimates
        raw = n * 3600.0 / max(eff_window, c.min_s)
        # EWMA once per second, so the smoothing doesn't depend on tick rate
        if now - self._last_update >= 1.0 or self.flow == 0:
            a = c.ewma_alpha if self.flow > 0 else 1.0
            self.flow = a * raw + (1 - a) * self.flow
            self._last_update = now
        density = in_approach / (approach_length_m / 1000.0) if approach_length_m > 0 else 0.0
        return FlowSnapshot(round(self.flow, 1), round(raw, 1), round(eff_window, 1), n,
                            round(density, 1), in_approach)
