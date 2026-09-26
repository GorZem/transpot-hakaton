from hypothesis import given, settings
from hypothesis import strategies as st

from smartcross.config import Config
from smartcross.controller import timing
from smartcross.controller.fsm import Controller, Mode, Observation, Phase
from smartcross.controller.safety import ConflictMonitor

DT = 0.1


def run(ctrl: Controller, obs_fn, seconds: float, t0: float = 0.0, monitor: ConflictMonitor | None = None):
    """Run the controller; obs_fn(t, ctrl) -> Observation. Returns list of (t, phase)."""
    trace = []
    n = int(seconds / DT)
    for i in range(n):
        t = t0 + i * DT
        st_ = ctrl.tick(t, obs_fn(t, ctrl))
        if monitor:
            monitor.check(t, st_.phase)
        trace.append((t, st_.phase))
    return trace


def first_time(trace, phase):
    return next((t for t, p in trace if p == phase), None)


def test_starts_safe_and_goes_to_vehicle_green():
    cfg = Config()
    c = Controller(cfg)
    assert c.state.phase == Phase.ALL_RED_TO_VEH
    trace = run(c, lambda t, _: Observation(), 5)
    assert first_time(trace, Phase.VEH_GREEN) is not None


def test_no_pedestrians_vehicles_keep_green():
    c = Controller(Config())
    trace = run(c, lambda t, _: Observation(veh_in_approach=3, veh_flow_vph=800), 600)
    assert first_time(trace, Phase.PED_GREEN) is None


def test_empty_road_pedestrian_served_quickly():
    cfg = Config()
    c = Controller(cfg)
    run(c, lambda t, _: Observation(), 30)  # settle in vehicle green, road empty
    trace = run(c, lambda t, _: Observation(ped_waiting={"A": 1}), 30, t0=30)
    t_green = first_time(trace, Phase.PED_GREEN) - 30
    t = cfg.timing
    expected = (t.demand_debounce_s + t.min_ped_delay_empty_s
                + t.veh_green_blink_s + t.veh_yellow_s + t.all_red_s)
    assert t_green <= expected + 0.5


def test_heavy_traffic_delay_follows_webster_and_is_bounded():
    cfg = Config()
    heavy = Observation(ped_waiting={"A": 1}, veh_in_approach=5, veh_flow_vph=3000)
    c = Controller(cfg)
    run(c, lambda t, _: Observation(veh_in_approach=5, veh_flow_vph=3000), 60)
    trace = run(c, lambda t, _: heavy, 200, t0=60)
    wait = first_time(trace, Phase.VEH_GREEN_BLINK) - 60
    target = timing.target_ped_delay_s(cfg, 3000, 1)
    assert abs(wait - target) < 0.5  # wait is counted from the pedestrian's arrival
    assert wait <= cfg.timing.max_ped_wait_s + cfg.timing.demand_debounce_s + 0.2


def test_group_gets_priority():
    cfg = Config()

    def wait_for(n):
        c = Controller(cfg)
        run(c, lambda t, _: Observation(veh_in_approach=4, veh_flow_vph=3500), 60)
        tr = run(c, lambda t, _: Observation(ped_waiting={"A": n}, veh_in_approach=4, veh_flow_vph=3500), 200, t0=60)
        return first_time(tr, Phase.VEH_GREEN_BLINK) - 60

    assert wait_for(8) < wait_for(1)


def test_group_gets_longer_green():
    cfg = Config()
    assert timing.ped_green_s(cfg, 20) > timing.ped_green_s(cfg, 1)


def test_gap_out_switches_early():
    cfg = Config()

    def switch_time(gap: bool):
        c = Controller(cfg)
        run(c, lambda t, _: Observation(veh_in_approach=4, veh_flow_vph=2500), 60)
        # with gap=True traffic stops arriving 8 s after the call (smoothed flow is still high)
        obs = lambda t, _: Observation(ped_waiting={"A": 2}, veh_flow_vph=2500,
                                       veh_in_approach=0 if gap and t >= 68 else 4)
        return first_time(run(c, obs, 200, t0=60), Phase.VEH_GREEN_BLINK)

    assert switch_time(gap=True) < switch_time(gap=False) - 3


def test_emergency_vehicle_holds_green():
    cfg = Config()
    c = Controller(cfg)
    run(c, lambda t, _: Observation(), 30)
    tr = run(c, lambda t, _: Observation(ped_waiting={"A": 1}, emergency=t < 50), 60, t0=30)
    assert first_time(tr, Phase.VEH_GREEN_BLINK) >= 50


def test_emergency_hold_is_limited():
    cfg = Config()
    c = Controller(cfg)
    run(c, lambda t, _: Observation(), 30)
    tr = run(c, lambda t, _: Observation(ped_waiting={"A": 1}, emergency=True), 120, t0=30)
    sw = first_time(tr, Phase.VEH_GREEN_BLINK)
    assert sw is not None and sw <= 30 + cfg.control.emergency_hold_max_s + 1.5


def test_clearance_extended_while_people_on_crossing():
    cfg = Config()
    c = Controller(cfg)
    run(c, lambda t, _: Observation(), 30)
    tr = run(c, lambda t, _: Observation(ped_waiting={"A": 1}), 60, t0=30)
    # find when the all-red after pedestrians starts, then keep someone on the crossing
    c2 = Controller(cfg)
    run(c2, lambda t, _: Observation(), 30)
    tr2 = run(c2, lambda t, ctrl: Observation(ped_waiting={"A": 1} if t < 35 else {},
                                              ped_on_crossing=1 if t >= 35 else 0), 60, t0=30)
    def all_red_len(trace):
        phases = [p for _, p in trace]
        i = phases.index(Phase.ALL_RED_TO_VEH, phases.index(Phase.PED_GREEN))
        j = phases.index(Phase.VEH_GREEN, i)
        return trace[j][0] - trace[i][0]
    assert all_red_len(tr2) > all_red_len(tr) + 5


def test_fixed_mode_cycles_without_pedestrians():
    cfg = Config()
    c = Controller(cfg, mode=Mode.FIXED)
    tr = run(c, lambda t, _: Observation(ped_vision_ok=False, veh_vision_ok=False), 300)
    n_ped = sum(1 for (a, b) in zip(tr, tr[1:]) if a[1] != Phase.PED_GREEN and b[1] == Phase.PED_GREEN)
    cycle = cfg.fallback.fixed.veh_green_s + timing.ped_phase_total_s(cfg)
    assert n_ped >= int(300 / cycle) - 1


def test_degraded_mode_guarantees_recall():
    cfg = Config()
    c = Controller(cfg, mode=Mode.DEGRADED)
    tr = run(c, lambda t, _: Observation(veh_in_approach=3, veh_flow_vph=500, ped_vision_ok=False), 300)
    starts = [b[0] for a, b in zip(tr, tr[1:]) if a[1] != Phase.PED_GREEN and b[1] == Phase.PED_GREEN]
    assert len(starts) >= 3


def test_flashing_entered_only_from_vehicle_green_and_left_via_all_red():
    cfg = Config()
    c = Controller(cfg)
    run(c, lambda t, _: Observation(), 30)
    run(c, lambda t, _: Observation(ped_waiting={"A": 1}), 6, t0=30)
    assert c.state.phase != Phase.VEH_GREEN
    c.set_mode(Mode.FLASHING)
    tr = run(c, lambda t, _: Observation(), 60, t0=36)
    phases = [p for _, p in tr]
    i = phases.index(Phase.FLASHING)
    assert phases[i - 1] == Phase.VEH_GREEN
    assert Phase.PED_GREEN in phases[:i]  # pedestrian phase completed first
    c.set_mode(Mode.ADAPTIVE)
    tr = run(c, lambda t, _: Observation(), 10, t0=96)
    assert tr[0][1] == Phase.ALL_RED_TO_VEH


def test_capacity_green_keeps_degree_of_saturation():
    cfg = Config()
    for q in (1500, 3000, 4500):
        g = timing.capacity_green_s(cfg, q)
        y = timing.flow_ratio(cfg, q)
        x = y * (g + timing.ped_phase_total_s(cfg)) / g
        assert x <= cfg.control.max_saturation + 1e-6


def test_switch_when_accumulated_delay_reaches_phase_cost():
    cfg = Config()
    q = 800
    c = Controller(cfg)
    run(c, lambda t, _: Observation(veh_in_approach=4, veh_flow_vph=q), 60)
    tr = run(c, lambda t, _: Observation(ped_waiting={"A": 1}, veh_in_approach=4, veh_flow_vph=q), 200, t0=60)
    wait = first_time(tr, Phase.VEH_GREEN_BLINK) - 60
    assert abs(wait - timing.expected_ped_delay_s(cfg, q, 1)) < 1.0


def test_more_pedestrians_switch_sooner():
    cfg = Config()

    def wait_for(n):
        c = Controller(cfg)
        run(c, lambda t, _: Observation(veh_in_approach=4, veh_flow_vph=2000), 60)
        tr = run(c, lambda t, _: Observation(ped_waiting={"A": n}, veh_in_approach=4, veh_flow_vph=2000), 200, t0=60)
        return first_time(tr, Phase.VEH_GREEN_BLINK) - 60

    waits = [wait_for(n) for n in (1, 2, 4)]
    assert waits[0] > waits[1] > waits[2]


def test_delay_grows_with_flow():
    cfg = Config()
    d = [timing.target_ped_delay_s(cfg, q, 1) for q in range(0, 7000, 250)]
    assert all(a <= b for a, b in zip(d, d[1:]))
    assert d[0] == cfg.timing.veh_min_green_s and d[-1] == cfg.timing.max_ped_wait_s


def test_webster_monotonic_in_flow():
    cfg = Config()
    cycles = [timing.webster_cycle_s(cfg, q) for q in range(0, 8000, 200)]
    assert all(a <= b for a, b in zip(cycles, cycles[1:]))
    assert cycles[-1] == cfg.control.cycle_max_s


# ---------------------------------------------------------------- property-based
obs_strategy = st.builds(
    Observation,
    ped_waiting=st.dictionaries(st.sampled_from(["A", "B"]), st.integers(0, 15), max_size=2),
    ped_on_crossing=st.integers(0, 5),
    veh_in_approach=st.integers(0, 10),
    veh_flow_vph=st.floats(0, 6000),
    emergency=st.booleans(),
    button=st.booleans(),
    ped_vision_ok=st.booleans(),
    veh_vision_ok=st.booleans(),
)


@settings(max_examples=150, deadline=None)
@given(steps=st.lists(st.tuples(obs_strategy, st.integers(1, 80), st.sampled_from(list(Mode)) | st.none()),
                      min_size=1, max_size=40))
def test_never_conflicting_signals(steps):
    cfg = Config()
    c = Controller(cfg)
    mon = ConflictMonitor(cfg)
    t = 0.0
    for obs, ticks, mode in steps:
        if mode is not None:
            c.set_mode(mode)
        for _ in range(ticks):
            st_ = c.tick(t, obs)
            assert not mon.check(t, st_.phase), mon.violations
            t += 0.25


@settings(max_examples=100, deadline=None)
@given(flow=st.floats(0, 6000), n=st.integers(1, 30), cars=st.integers(0, 10))
def test_pedestrian_wait_never_exceeds_limit(flow, n, cars):
    """Without emergency vehicles a waiting pedestrian always gets green within max_ped_wait."""
    cfg = Config()
    c = Controller(cfg)
    run(c, lambda t, _: Observation(veh_in_approach=cars, veh_flow_vph=flow), 40)
    tr = run(c, lambda t, _: Observation(ped_waiting={"A": n}, veh_in_approach=cars, veh_flow_vph=flow), 150, t0=40)
    t = cfg.timing
    bound = t.max_ped_wait_s + t.demand_debounce_s + t.veh_green_blink_s + t.veh_yellow_s + t.all_red_s + 0.5
    # the pedestrian arrived at t=40 possibly during the minimum green of a fresh vehicle phase
    green_at = first_time(tr, Phase.PED_GREEN)
    assert green_at is not None and green_at - 40 <= bound


def test_monitor_does_not_judge_first_observed_phase():
    cfg = Config()
    mon = ConflictMonitor(cfg)
    assert not mon.check(10.0, Phase.ALL_RED_TO_VEH)
    assert not mon.check(10.5, Phase.VEH_GREEN)      # started before the monitor: unknown length
    assert not mon.check(20.0, Phase.VEH_GREEN_BLINK)
    assert mon.check(20.5, Phase.VEH_YELLOW)          # blink too short -> violation


def test_monitor_detects_conflict_and_illegal_transition():
    mon = ConflictMonitor(Config())
    mon.check(0, Phase.VEH_GREEN)
    assert any("недопустимый" in v for v in mon.check(100, Phase.PED_GREEN))
