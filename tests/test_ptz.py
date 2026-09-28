"""Поворот камер: калибровка после поворота, перенос зон, фиксированный план при непригодном изображении."""
import json
import time
import types
from pathlib import Path

import numpy as np

from node.model import Mode
from node.runtime import SiteRuntime
from node.vision.geometry import CameraModel, reproject_zones

SITES = {s["id"]: s for s in json.loads((Path(__file__).parent.parent / "data" / "sites.json").read_text(encoding="utf-8"))["sites"]}
SITE = SITES["p-krasnodonskaya-mid"]


def test_pan_rotates_axis_around_vertical():
    home = CameraModel(SITE["cameras"][0])
    cam = home.turned(30, 0)
    assert abs(((cam.azimuth_deg - home.azimuth_deg) % 360) - 30) < 1e-6
    assert abs(cam.tilt_down_deg - home.tilt_down_deg) < 1e-6
    # точка на земле по новой оси — в центре кадра
    d = cam.f[:2] / np.hypot(*cam.f[:2])
    ground = cam.pos[:2] + d * cam.pos[2] / np.tan(np.radians(cam.tilt_down_deg))
    px, front = cam.ground_to_pixels(ground[None, :])
    assert front[0] and np.allclose(px[0], [cam.w / 2, cam.h / 2], atol=0.5)


def test_zones_follow_the_ground_after_turn():
    """Зона, перенесённая после поворота, накрывает на земле то же место."""
    home = CameraModel(SITE["cameras"][0])
    rt = SiteRuntime(SITE, None, None)
    zones = rt.perception.geo.auto_zones(home)
    turned = home.turned(20, 5)
    moved = reproject_zones(zones, home, turned)
    assert moved
    for z in moved:
        src = next(x for x in zones if x["key"] == z["key"])
        a = home.pixels_to_ground(np.array(src["points"]) * [home.w, home.h])
        b = turned.pixels_to_ground(np.array(z["points"]) * [turned.w, turned.h])
        a, b = a[~np.isnan(a[:, 0])], b[~np.isnan(b[:, 0])]
        # центр зоны на земле почти не сместился (края могли обрезаться кадром)
        assert np.hypot(*(a.mean(axis=0) - b.mean(axis=0))) < 6.0
    # поворот туда и обратно возвращает зоны
    back = reproject_zones(reproject_zones(zones, home, home.turned(10, 0)), home.turned(10, 0), home)
    assert len(back) >= len(zones) - 1


def _runtime():
    rt = SiteRuntime(SITE, None, None)
    rt.worker = types.SimpleNamespace(error=None, ready=types.SimpleNamespace(is_set=lambda: True), paused=False,
                                      last_cycle=time.monotonic())
    now = time.monotonic()
    for c in rt.streams:
        rt.cam_fault[c] = None
        rt._last_seen[c] = rt._last_result[c] = now
    return rt


def test_turning_camera_switches_to_fixed_and_recalibrates():
    rt = _runtime()
    cid = next(iter(rt.streams))
    rt.ptz = types.SimpleNamespace(state={cid: {"pan_deg": 15.0, "tilt_deg": 0.0, "moving": True}}, stop=lambda: None)
    t = time.monotonic()
    rt._check_ptz(t)
    rt._check_warn(t)
    assert rt._choose_mode()[0] == Mode.FIXED and rt.cam_state(cid)[1] == "warn"
    rt.ptz.state[cid]["moving"] = False
    rt._check_ptz(t + 0.5)            # ещё не успокоилась
    assert rt.cam_pose[cid] == (0.0, 0.0)
    rt._check_ptz(t + 3.0)
    rt._check_warn(t + 3.0)
    assert rt.cam_pose[cid] == (15.0, 0.0)
    assert rt._choose_mode()[0] == Mode.ADAPTIVE
    assert any("пересчитаны" in e["message"] for e in rt.recent)


def test_no_usable_picture_means_fixed_plan():
    rt = _runtime()
    cams = list(rt.streams)
    t = time.monotonic()
    rt._check_warn(t)
    assert rt._choose_mode()[0] == Mode.ADAPTIVE
    # одна камера в тумане — деградированный, обе — фиксированный план
    rt.cam_fault[cams[0]] = "blind"
    assert rt._choose_mode()[0] == Mode.DEGRADED and rt.cam_state(cams[0])[1] == "warn"
    rt.cam_fault[cams[1]] = "black"
    assert rt._choose_mode()[0] == Mode.FIXED
    # камеры целы, но никого не видят дольше порога
    rt = _runtime()
    t = time.monotonic() + rt.p.empty_scene_s + 1
    rt.worker.last_cycle = t
    for c in cams:
        rt._last_result[c] = t
    rt._check_warn(t)
    assert all(rt.cam_state(c)[0] == "empty" for c in cams)
    assert rt._choose_mode()[0] == Mode.FIXED
    # распознавание остановилось
    rt = _runtime()
    rt.worker.paused = True
    rt._check_warn(time.monotonic())
    mode, reason = rt._choose_mode()
    assert mode == Mode.FIXED and "компьютерное зрение" in reason
