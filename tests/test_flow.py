import random

from smartcross.config import FlowWindowConfig
from smartcross.traffic.flow import FlowEstimator


def simulate(rate_vph, seconds, est=None, t0=0.0, seed=1):
    rnd = random.Random(seed)
    est = est or FlowEstimator(FlowWindowConfig(), start=t0)
    t = t0
    next_car = t0 + rnd.expovariate(rate_vph / 3600)
    snap = None
    while t < t0 + seconds:
        while next_car <= t:
            est.add_vehicle(next_car)
            next_car += rnd.expovariate(rate_vph / 3600)
        snap = est.update(t)
        t += 0.5
    return est, snap


def test_flow_estimate_accuracy():
    for rate in (300, 1200, 3000):
        _, snap = simulate(rate, 1800)
        assert abs(snap.flow_vph - rate) / rate < 0.3, (rate, snap)


def test_window_adapts_to_density():
    _, dense = simulate(3000, 1200)
    _, sparse = simulate(60, 1200)
    assert dense.window_s < 120
    assert sparse.window_s == FlowWindowConfig().max_s


def test_flow_follows_change():
    est, _ = simulate(300, 1200)
    _, snap = simulate(2400, 600, est=est, t0=1200, seed=2)
    assert snap.flow_vph > 1500


def test_density():
    est = FlowEstimator(FlowWindowConfig())
    assert est.update(10, in_approach=5, approach_length_m=50).density_vpkm == 100
