"""Спецтранспорт: синяя машина — не спецтранспорт, и перекрёсток не встаёт намертво."""
import json
from pathlib import Path

import numpy as np

from node.control import EMERGENCY_MAX_HOLD_S, Controller
from node.model import Observation, Veh, build_layout
from node.params import Params
from node.perception import Perception
from node.vision.detector import Detection
from node.vision.geometry import CameraModel

SITES = {s["id"]: s for s in json.loads((Path(__file__).parent.parent / "data" / "sites.json").read_text(encoding="utf-8"))["sites"]}
CROSS = SITES["x-krasnodarskaya-novorossiyskaya"]


class FakeFrame:
    def __init__(self, ts):
        self.ts = ts


def car_at(cam: CameraModel, en, beacon):
    (u, v) = cam.ground_to_pixels(np.array([en]))[0][0]
    return Detection("vehicle", 2, 0.9, (u - 20, v - 30, u + 20, v), beacon=beacon)


def run_car(colors, s_from_stop=30.0):
    """Машина стоит на подходе в s_from_stop м перед стоп-линией; маячок по кадрам — colors."""
    layout = build_layout(CROSS)
    cams = [CameraModel(c) for c in CROSS["cameras"]]
    per = Perception(CROSS, layout, cams, Params())
    ap = per.geo.approaches[0]
    d = (ap.pts[1] - ap.pts[0]) / np.hypot(*(ap.pts[1] - ap.pts[0]))
    nrm = np.array([-d[1], d[0]])
    spot = ap.pts[0] + d * (ap.stop_s + s_from_stop) + nrm * ap.in_width / 2
    cam = next(c for c in cams if c.sees(np.array([spot]))[0])
    t, up = 1000.0, None
    for c in colors:
        per.push(cam.id, FakeFrame(t), [car_at(cam, spot, c)])
        up = per.update({cam.id}, {})
        t += 0.4
    return up.obs, ap.group


def test_blue_car_is_not_emergency():
    obs, _ = run_car(["blue"] * 10)
    assert not obs.emergency


def test_flashing_beacon_is_emergency():
    obs, g = run_car(["blue", "red", "blue", "red", "blue", "red"])
    assert g in obs.emergency


def test_emergency_inside_junction_does_not_hold():
    obs, _ = run_car(["blue", "red", "blue", "red", "blue", "red"], s_from_stop=-5)
    assert not obs.emergency


def test_emergency_hold_is_limited():
    """Спецтранспорт «застрял»: через EMERGENCY_MAX_HOLD_S приоритет снимается и фазы снова меняются."""
    L = build_layout(CROSS)
    c = Controller(L, Params(), 0.0)
    a, b = L.stages[0].veh[0], L.stages[1].veh[0]
    obs = Observation(queue={a: 0, b: 6}, eta={a: None, b: 1.0}, flow_vph={a: 300, b: 600},
                      waiting={g: 0 for g in L.ped_groups()}, max_wait={g: 0 for g in L.ped_groups()},
                      on_crosswalk={g: 0 for g in L.ped_groups()}, emergency={a})
    t, switched = 0.0, None
    while t < 200:
        sig = c.tick(t, obs)
        if t > 20 and sig[a] in (Veh.GREEN_BLINK, Veh.YELLOW) and switched is None:
            switched = t
        t = round(t + 0.1, 6)
    assert switched is not None and switched < EMERGENCY_MAX_HOLD_S + 30
    assert any("приоритет снят" in e.message for e in c.drain_events())
