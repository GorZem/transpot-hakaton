"""Оценка интенсивности движения с адаптивным окном усреднения.

Окно растягивается, пока в него не попадёт N машин (по умолчанию 20), но остаётся в пределах
[мин; макс]. При пуассоновском потоке относительная ошибка оценки около 1/√N, поэтому на
плотном потоке окно короткое и оценка быстро реагирует, а на редком — длинное и не скачет.
"""
from __future__ import annotations

from collections import deque

from node.params import Params


class FlowEstimator:
    def __init__(self, start: float):
        self.start = start
        self.times: deque[float] = deque()
        self.window_s = 0.0

    def add(self, t: float, n: int = 1) -> None:
        self.times.extend([t] * n)

    def estimate(self, now: float, p: Params) -> float:
        while self.times and self.times[0] < now - p.flow_max_window_s:
            self.times.popleft()
        n = p.flow_target_count
        span = max(p.flow_min_window_s, now - self.start)  # после запуска данных меньше
        if len(self.times) >= n and now - self.times[-n] >= p.flow_min_window_s:
            # Окно ровно на N машин: несмещённая оценка (N-1)/T.
            w = now - self.times[-n]
            if w <= min(p.flow_max_window_s, span):
                self.window_s = w
                return (n - 1) / w * 3600.0
        w = min(p.flow_min_window_s if len(self.times) >= n else p.flow_max_window_s, span)
        self.window_s = w
        count = sum(1 for t in self.times if t >= now - w)
        return count / w * 3600.0
