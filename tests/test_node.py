import random
from datetime import datetime

import pytest

from node.control import Controller
from node.flow import FlowEstimator
from node.model import Mode, Observation, Ped, Veh, build_layout
from node.params import Params
from node.runtime import SiteRuntime
from node.safety import Safety

SITE = {"crossing": {"id": "t-crossing", "kind": "crossing", "road_bearing_deg": 2.0},
        "tee": {"id": "t-tee", "kind": "tee", "road_bearing_deg": 84.0},
        "cross": {"id": "t-cross", "kind": "cross", "road_bearing_deg": 3.0}}
PEAK = datetime(2026, 10, 1, 8, 30)
NIGHT = datetime(2026, 10, 1, 3, 0)


def run(rt: SiteRuntime, seconds: float, start: float = 0.0, wall=PEAK, dt: float = 0.1) -> float:
    k0 = round(start / dt)
    for k in range(k0, k0 + round(seconds / dt)):
        rt.tick(k * dt, dt, wall)
        assert rt.trip is None, rt.trip
    return (k0 + round(seconds / dt)) * dt


@pytest.mark.parametrize("kind", ["crossing", "tee", "cross"])
def test_random_faults_never_trip_safety(kind):
    """Случайные отказы камер, смены режима оператором, группы и спецтранспорт: защита не срабатывает."""
    rnd = random.Random(kind)
    rt = SiteRuntime(dict(SITE[kind], id=f"rand-{kind}"))
    t = 0.0
    for _ in range(40):
        action = rnd.choice(["cam", "cam", "mode", "group", "emergency", "none"])
        if action == "cam":
            rt.set_camera(rnd.choice(["cam1", "cam2"]), rnd.random() < 0.5)
        elif action == "mode":
            rt.set_forced_mode(rnd.choice([None, None, Mode.FIXED, Mode.FLASHING]))
        elif action == "group":
            rt.demo_group(rnd.randint(3, 12))
        elif action == "emergency":
            rt.demo_emergency()
        t = run(rt, rnd.uniform(5, 60), t, wall=rnd.choice([PEAK, NIGHT]))


@pytest.mark.parametrize("kind", ["crossing", "tee", "cross"])
def test_pedestrians_are_served(kind):
    rt = SiteRuntime(dict(SITE[kind]))
    served, waits = 0, 0.0
    t = 0.0
    for _ in range(30):
        t = run(rt, 60, t)
        m = rt.take_minute()
        served += m.ped_served
        waits += m.wait_sum
    assert served > 30
    assert waits / served < 40


def test_empty_road_pedestrian_gets_green_fast():
    L = build_layout("crossing", 0)
    p = Params()
    c = Controller(L, p, 0.0)
    empty = Observation(waiting={"cw": 0}, max_wait={"cw": 0}, on_crosswalk={"cw": 0},
                        queue={"n": 0, "s": 0}, eta={"n": None, "s": None}, flow_vph={"n": 0, "s": 0},
                        cameras={"cam1": True, "cam2": True})
    t = 0.0
    while t < 30:  # запуск, машины стоят в зелёном
        c.tick(t, empty)
        t = round(t + 0.1, 6)
    arrive = t
    while c.signals["cw"] != Ped.GREEN:
        waited = t - arrive
        obs = Observation(**{**empty.__dict__, "waiting": {"cw": 1}, "max_wait": {"cw": waited}})
        c.tick(t, obs)
        t = round(t + 0.1, 6)
        assert t - arrive < 20
    # задержка 3 с + мигание 3 + жёлтый 3 + все красные 2
    assert t - arrive <= p.delay_min_s + p.green_blink_s + p.yellow_s + p.all_red_s + 0.5


def test_group_is_served_faster_than_single_on_busy_road():
    def time_to_green(n: int) -> float:
        L, p = build_layout("crossing", 0), Params()
        c = Controller(L, p, 0.0)
        busy = dict(queue={"n": 3, "s": 2}, eta={"n": 1.5, "s": 2.0}, flow_vph={"n": 700, "s": 700},
                    on_crosswalk={"cw": 0}, cameras={"cam1": True, "cam2": True})
        t = 0.0
        while t < 20:
            c.tick(t, Observation(waiting={"cw": 0}, max_wait={"cw": 0}, **busy))
            t = round(t + 0.1, 6)
        arrive = t
        while c.signals["cw"] != Ped.GREEN:
            c.tick(t, Observation(waiting={"cw": n}, max_wait={"cw": t - arrive}, **busy))
            t = round(t + 0.1, 6)
        return t - arrive

    single, group = time_to_green(1), time_to_green(10)
    assert group < single - 10


def test_safety_detects_conflict_and_short_yellow():
    L, p = build_layout("crossing", 0), Params()
    s = Safety(L, p)
    assert s.check(0.0, {"n": Veh.RED, "s": Veh.RED, "cw": Ped.RED}) == []
    errs = s.check(0.1, {"n": Veh.RED_YELLOW, "s": Veh.RED, "cw": Ped.GREEN})
    assert not errs  # красный с жёлтым не разрешает движение
    errs = s.check(0.2, {"n": Veh.GREEN, "s": Veh.RED, "cw": Ped.GREEN})
    assert any("конфликт" in e for e in errs)
    s2 = Safety(L, p)
    s2.check(0.0, {"n": Veh.GREEN, "s": Veh.GREEN, "cw": Ped.RED})
    s2.check(1.0, {"n": Veh.GREEN_BLINK, "s": Veh.GREEN_BLINK, "cw": Ped.RED})
    s2.check(4.0, {"n": Veh.YELLOW, "s": Veh.YELLOW, "cw": Ped.RED})
    errs = s2.check(5.0, {"n": Veh.RED, "s": Veh.RED, "cw": Ped.RED})
    assert any("жёлт" in e or "yellow" in e for e in errs)
    errs = s2.check(5.5, {"n": Veh.RED, "s": Veh.RED, "cw": Ped.GREEN})
    assert any("≥" in e for e in errs)  # зелёный пешеходам раньше, чем через «все красные»


def test_all_cameras_down_switches_to_fixed_and_back():
    rt = SiteRuntime(dict(SITE["crossing"], id="cams"))
    t = run(rt, 30)
    rt.set_camera("cam1", False)
    t = run(rt, 5, t)
    assert rt.ctrl.mode == Mode.DEGRADED
    rt.set_camera("cam2", False)
    t = run(rt, 5, t)
    assert rt.ctrl.mode == Mode.FIXED
    rt.set_camera("cam1", True)
    rt.set_camera("cam2", True)
    run(rt, 5, t)
    assert rt.ctrl.mode == Mode.ADAPTIVE


@pytest.mark.parametrize("vph", [100, 300, 1200, 2400])
def test_flow_estimate_accuracy(vph):
    """Средняя ошибка по многим замерам мала, окно в заданных пределах и короче на плотном потоке."""
    p = Params()
    rnd = random.Random(vph)
    f = FlowEstimator(0.0)
    t, dt, errs, windows = 0.0, 0.1, [], []
    while t < 7200:
        if rnd.random() < vph / 3600 * dt:
            f.add(t)
        t += dt
        if t > 900 and int(t * 10) % 600 == 0:  # раз в минуту
            errs.append(f.estimate(t, p) / vph - 1)
            windows.append(f.window_s)
    bias = sum(errs) / len(errs)
    spread = (sum(e * e for e in errs) / len(errs)) ** 0.5
    assert abs(bias) < 0.1
    assert spread < 0.3
    assert all(p.flow_min_window_s <= w <= p.flow_max_window_s for w in windows)
