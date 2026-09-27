import json
import random
from pathlib import Path

import numpy as np
import pytest

from node.control import Controller
from node.flow import FlowEstimator
from node.model import Mode, Observation, Ped, Veh, build_layout
from node.params import Params
from node.perception import Perception
from node.safety import Safety
from node.vision.detector import Detection
from node.vision.geometry import CameraModel, SiteGeometry

SITES = {s["id"]: s for s in json.loads((Path(__file__).parent.parent / "data" / "sites.json").read_text(encoding="utf-8"))["sites"]}
KINDS = {"crossing": "p-krasnodonskaya-mid", "tee": "t-novorossiyskaya-stavropolskaya", "cross": "x-krasnodarskaya-krasnodonskaya"}


def random_obs(L, rnd: random.Random, blind: set[str] = frozenset()) -> Observation:
    o = Observation()
    for g in L.ped_groups():
        if g in blind:
            o.waiting[g] = o.max_wait[g] = o.on_crosswalk[g] = None
        else:
            n = rnd.choice([0, 0, 1, 2, 3, 8])
            o.waiting[g], o.max_wait[g], o.on_crosswalk[g] = n, rnd.uniform(0, 70) if n else 0.0, rnd.choice([0, 0, 1])
    for g in L.veh_groups():
        if g in blind:
            o.queue[g] = o.eta[g] = o.flow_vph[g] = None
        else:
            o.queue[g] = rnd.choice([0, 0, 1, 4])
            o.eta[g] = rnd.choice([None, 1.5, 4.0, 9.0])
            o.flow_vph[g] = rnd.uniform(0, 1400)
            if rnd.random() < 0.03:
                o.emergency.add(g)
    return o


@pytest.mark.parametrize("kind", ["crossing", "tee", "cross"])
def test_random_observations_never_trip_safety(kind):
    """Случайные наблюдения, «слепые» зоны и смены режимов: модуль безопасности ни разу не срабатывает."""
    L = build_layout(SITES[KINDS[kind]])
    p = Params()
    rnd = random.Random(kind)
    c, s = Controller(L, p, 0.0), Safety(L, p)
    blind: set[str] = set()
    obs = random_obs(L, rnd)
    t = 0.0
    for k in range(60000):  # около 1 ч 40 мин модельного времени
        if k % 50 == 0:
            obs = random_obs(L, rnd, blind)
        if k % 3000 == 0:
            blind = set(rnd.sample(L.ped_groups() + L.veh_groups(), rnd.randint(0, 2)))
            c.set_mode(rnd.choice([Mode.ADAPTIVE, Mode.ADAPTIVE, Mode.DEGRADED, Mode.FIXED, Mode.FLASHING]), "тест", t)
        sig = c.tick(t, obs)
        assert s.check(t, sig) == [], (k, sig)
        t = round(t + 0.1, 6)


@pytest.mark.parametrize("kind", ["crossing", "tee", "cross"])
def test_every_pedestrian_group_gets_green(kind):
    L = build_layout(SITES[KINDS[kind]])
    c = Controller(L, Params(), 0.0)
    served: set[str] = set()
    arrived = {g: 0.0 for g in L.ped_groups()}
    t = 0.0
    for _ in range(3000):
        o = Observation(queue={g: 2 for g in L.veh_groups()}, eta={g: 2.0 for g in L.veh_groups()},
                        flow_vph={g: 600 for g in L.veh_groups()})
        for g in L.ped_groups():  # двое ждут с момента прихода, ожидание растёт
            w = t - arrived[g]
            o.waiting[g], o.max_wait[g], o.wait_sum[g], o.on_crosswalk[g] = 2, w, 2 * w, 0
        sig = c.tick(t, o)
        for g in L.ped_groups():
            if sig[g] == Ped.GREEN:
                served.add(g)
                arrived[g] = t
        t = round(t + 0.1, 6)
    assert served == set(L.ped_groups())


def test_empty_road_pedestrian_gets_green_fast():
    L, p = build_layout(SITES[KINDS["crossing"]]), Params()
    c = Controller(L, p, 0.0)
    base = dict(on_crosswalk={"ped": 0}, queue={"veh": 0}, eta={"veh": None}, flow_vph={"veh": 0})
    t = 0.0
    while t < 30:
        c.tick(t, Observation(waiting={"ped": 0}, max_wait={"ped": 0}, **base))
        t = round(t + 0.1, 6)
    arrive = t
    while c.signals["ped"] != Ped.GREEN:
        c.tick(t, Observation(waiting={"ped": 1}, max_wait={"ped": t - arrive}, **base))
        t = round(t + 0.1, 6)
        assert t - arrive < 20
    assert t - arrive <= p.delay_min_s + p.green_blink_s + p.yellow_s + p.all_red_s + 0.5


def test_group_is_served_faster_than_single_on_busy_road():
    def time_to_green(n: int) -> float:
        L = build_layout(SITES[KINDS["crossing"]])
        c = Controller(L, Params(), 0.0)
        busy = dict(queue={"veh": 5}, eta={"veh": 1.5}, flow_vph={"veh": 1400}, on_crosswalk={"ped": 0})
        t = 0.0
        while t < 20:
            c.tick(t, Observation(waiting={"ped": 0}, max_wait={"ped": 0}, **busy))
            t = round(t + 0.1, 6)
        arrive = t
        while c.signals["ped"] != Ped.GREEN:
            c.tick(t, Observation(waiting={"ped": n}, max_wait={"ped": t - arrive}, **busy))
            t = round(t + 0.1, 6)
        return t - arrive

    assert time_to_green(10) < time_to_green(1) - 10


def test_takeover_starts_from_device_stage():
    L = build_layout(SITES[KINDS["cross"]])
    c, s = Controller(L, Params(), 0.0), Safety(L, Params())
    c.adopt(1, {"ped_A"}, 5.0)
    assert c.signals["veh_B"] == Veh.GREEN and c.signals["ped_A"] == Ped.GREEN and c.signals["veh_A"] == Veh.RED
    assert s.check(5.0, c.tick(5.0, Observation())) == []


def test_safety_detects_conflict_and_short_yellow():
    L, p = build_layout(SITES[KINDS["crossing"]]), Params()
    s = Safety(L, p)
    assert s.check(0.0, {"veh": Veh.RED, "ped": Ped.RED}) == []
    errs = s.check(0.2, {"veh": Veh.GREEN, "ped": Ped.GREEN})
    assert any("конфликт" in e for e in errs)
    s2 = Safety(L, p)
    s2.check(0.0, {"veh": Veh.GREEN, "ped": Ped.RED})
    s2.check(1.0, {"veh": Veh.GREEN_BLINK, "ped": Ped.RED})
    s2.check(4.0, {"veh": Veh.YELLOW, "ped": Ped.RED})
    assert any("жёлтый" in e for e in s2.check(5.0, {"veh": Veh.RED, "ped": Ped.RED}))
    assert any("≥" in e for e in s2.check(5.5, {"veh": Veh.RED, "ped": Ped.GREEN}))


@pytest.mark.parametrize("vph", [100, 300, 1200, 2400])
def test_flow_estimate_accuracy(vph):
    p = Params()
    rnd = random.Random(vph)
    f = FlowEstimator(0.0)
    t, dt, errs, windows = 0.0, 0.1, [], []
    while t < 7200:
        if rnd.random() < vph / 3600 * dt:
            f.add(t)
        t += dt
        if t > 900 and int(t * 10) % 600 == 0:
            errs.append(f.estimate(t, p) / vph - 1)
            windows.append(f.window_s)
    assert abs(sum(errs) / len(errs)) < 0.1
    assert (sum(e * e for e in errs) / len(errs)) ** 0.5 < 0.3
    assert all(p.flow_min_window_s <= w <= p.flow_max_window_s for w in windows)


# ---------------- геометрия и восприятие ----------------

def test_camera_projection_roundtrip():
    site = SITES[KINDS["cross"]]
    for cam in site["cameras"]:
        m = CameraModel(cam)
        pts = np.array([[x, y] for x in range(-20, 21, 5) for y in range(-20, 21, 5)], float)
        px, front = m.ground_to_pixels(pts)
        inside = front & (px[:, 0] > 0) & (px[:, 0] < m.w) & (px[:, 1] > 0) & (px[:, 1] < m.h)
        back = m.pixels_to_ground(px[inside])
        assert inside.sum() > 10
        assert np.nanmax(np.hypot(*(back - pts[inside]).T)) < 0.05


class FakeFrame:
    def __init__(self, ts):
        self.ts = ts


def detection_at(cam: CameraModel, en, kind="person", height=1.7) -> Detection:
    (u, v) = cam.ground_to_pixels(np.array([en]))[0][0]
    (ut, vt) = cam.ground_to_pixels(np.array([en]), z=height)[0][0]
    half = max(4.0, (v - vt) * 0.2)
    return Detection(kind, 0 if kind == "person" else 2, 0.9, (u - half, vt, u + half, v))


def test_perception_counts_waiting_and_serves_pedestrian():
    site = SITES[KINDS["crossing"]]
    L = build_layout(site)
    cams = [CameraModel(c) for c in site["cameras"]]
    per = Perception(site, L, cams, Params())
    geo = SiteGeometry(site)
    cw = geo.crosswalks[0]
    spot = cw.wait[0].mean(axis=0)
    healthy = {c.id for c in cams}
    t = 1000.0
    for _ in range(12):  # стоит у перехода 3 с
        for c in cams:
            per.push(c.id, FakeFrame(t), [detection_at(c, spot)])
        up = per.update(healthy, {"veh": Veh.GREEN, "ped": Ped.RED})
        t += 0.25
    assert up.obs.waiting["ped"] == 1
    assert up.obs.max_wait["ped"] > 1.5
    served = []
    path = cw.b - spot
    steps = int(np.hypot(*path) / 0.35)  # 1,4 м/с при обновлении раз в 0,25 с
    for k in range(1, steps + 1):  # идёт через переход на зелёный
        p = spot + path * k / steps
        for c in cams:
            per.push(c.id, FakeFrame(t), [detection_at(c, p)])
        up = per.update(healthy, {"veh": Veh.RED, "ped": Ped.GREEN})
        served += up.served
        t += 0.25
    assert len(served) == 1 and served[0][0] == "ped" and served[0][2] is False
    assert up.obs.waiting["ped"] == 0


def test_perception_hides_zones_of_failed_camera():
    site = SITES[KINDS["cross"]]
    L = build_layout(site)
    cams = [CameraModel(c) for c in site["cameras"]]
    per = Perception(site, L, cams, Params())
    obs = per.refresh(0.0, set())
    assert all(v is None for v in obs.waiting.values()) and all(v is None for v in obs.queue.values())
    obs = per.refresh(0.0, {cams[0].id})
    assert any(v is not None for v in obs.waiting.values())


# ---------- алгоритм «минимум задержки людей» ----------
def _time_to_ped_green(n: int, vph: float, queue: int, eta: float | None, kind: str = "crossing") -> float:
    """Через сколько секунд после прихода n пешеходов им загорится зелёный."""
    L = build_layout(SITES[KINDS[kind]])
    c = Controller(L, Params(), 0.0)
    veh = dict(queue={g: queue for g in L.veh_groups()}, eta={g: eta for g in L.veh_groups()},
               flow_vph={g: vph for g in L.veh_groups()})
    t = 0.0
    while t < 30:  # запуск, транспорт в зелёном
        c.tick(t, Observation(waiting={g: 0 for g in L.ped_groups()}, max_wait={g: 0 for g in L.ped_groups()},
                              wait_sum={g: 0 for g in L.ped_groups()}, **veh))
        t = round(t + 0.1, 6)
    arrive = t
    g0 = L.ped_groups()[0]
    while c.signals[g0] != Ped.GREEN:
        w = t - arrive
        c.tick(t, Observation(waiting={g: n for g in L.ped_groups()}, max_wait={g: w for g in L.ped_groups()},
                              wait_sum={g: n * w for g in L.ped_groups()}, on_crosswalk={g: 0 for g in L.ped_groups()}, **veh))
        t = round(t + 0.1, 6)
        assert t - arrive < 120
    return t - arrive


def test_more_people_get_green_sooner():
    times = [_time_to_ped_green(n, 900, 2, 3.0) for n in (1, 2, 5, 10)]
    assert times == sorted(times, reverse=True) and times[0] > times[-1] + 20, times


def test_denser_traffic_delays_single_pedestrian_but_within_limit():
    quiet = _time_to_ped_green(1, 200, 0, None)
    busy = _time_to_ped_green(1, 1200, 4, 1.0)
    p = Params()
    assert quiet < 15 and busy > quiet + 20
    assert busy <= p.max_wait_s + p.green_blink_s + p.yellow_s + p.all_red_s + 0.5


def test_lets_single_approaching_car_pass_first():
    L = build_layout(SITES[KINDS["crossing"]])
    c = Controller(L, Params(), 0.0)
    base = dict(queue={"veh": 0}, flow_vph={"veh": 100})
    t = 0.0
    while t < 30:
        c.tick(t, Observation(waiting={"ped": 0}, max_wait={"ped": 0}, wait_sum={"ped": 0}, eta={"veh": None}, **base))
        t = round(t + 0.1, 6)
    # пешеход ждёт 5 с, машина в 2 с от стоп-линии: выгоднее пропустить её, чем остановить
    c.tick(t, Observation(waiting={"ped": 1}, max_wait={"ped": 5}, wait_sum={"ped": 5}, on_crosswalk={"ped": 0},
                          eta={"veh": 2.0}, **base))
    assert c.signals["veh"] == Veh.GREEN and "Пропускаем" in c.status


def test_intersection_long_red_queue_takes_over():
    L = build_layout(SITES[KINDS["cross"]])
    c = Controller(L, Params(), 0.0)
    t, switched = 0.0, None
    while t < 120:
        o = Observation(queue={"veh_A": 0, "veh_B": 6}, eta={"veh_A": 3.0, "veh_B": None},
                        flow_vph={"veh_A": 500, "veh_B": 500},
                        waiting={g: 0 for g in L.ped_groups()}, max_wait={g: 0 for g in L.ped_groups()},
                        wait_sum={g: 0 for g in L.ped_groups()}, on_crosswalk={g: 0 for g in L.ped_groups()})
        c.tick(t, o)
        if c.stage == 1 and switched is None:
            switched = t
        t = round(t + 0.1, 6)
    assert switched is not None and switched < 60
