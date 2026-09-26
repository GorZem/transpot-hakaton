"""Integration test: real camera threads on synthetic video, fake detector, failure injection."""
import time

import cv2
import numpy as np
import pytest

from smartcross.config import CameraConfig, Config, ConfigStore, FallbackConfig, TimingConfig, ZoneConfig
from smartcross.controller.fsm import Mode
from smartcross.engine import Engine
from smartcross.stats.db import StatsDB
from smartcross.vision.zones import Detection


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp("v") / "noise.avi"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (320, 240))
    rng = np.random.default_rng(0)
    for _ in range(50):
        w.write(rng.integers(40, 200, (240, 320, 3), dtype=np.uint8))
    w.release()
    return str(path)


class FakeDetector:
    """One pedestrian waiting on side A, one car driving through the approach zone."""

    def __init__(self):
        self.n = 0

    def __call__(self, frame):
        self.n += 1
        y = (self.n * 20) % 240
        return [Detection(1, "person", "pedestrian", (20, 150, 40, 200), 0.9),
                Detection(100 + self.n // 12, "car", "vehicle", (140, y - 30, 200, y), 0.9)]


def make_cfg(video):
    zones = [
        ZoneConfig(id="wait_a", type="ped_wait", side="A", points=[(0, 0.5), (0.2, 0.5), (0.2, 1), (0, 1)]),
        ZoneConfig(id="appr", type="approach", direction="1", length_m=40,
                   points=[(0.4, 0), (0.7, 0), (0.7, 0.9), (0.4, 0.9)]),
        ZoneConfig(id="line", type="count_line", direction="1", points=[(0.4, 0.5), (0.7, 0.5)]),
    ]
    return Config(
        cameras=[CameraConfig(id="c1", source=video, fps=10, zones=zones),
                 CameraConfig(id="c2", source=video, fps=10, zones=zones)],
        fallback=FallbackConfig(camera_timeout_s=0.8, frozen_s=1.0, recovery_s=1.0),
        timing=TimingConfig(max_ped_wait_s=15),
    )


def wait_for(pred, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_engine_modes_under_camera_failures(video, tmp_path):
    store = ConfigStore(tmp_path / "cfg.yaml")
    store.update(make_cfg(video))
    db = StatsDB(f"sqlite:///{tmp_path}/s.db")
    eng = Engine(store, db, detector_factory=lambda cfg: FakeDetector)
    eng.start()
    try:
        mode = lambda: eng.get_snapshot().get("mode")
        assert wait_for(lambda: mode() == Mode.ADAPTIVE.value), eng.get_snapshot()
        assert wait_for(lambda: eng.get_snapshot()["obs"]["ped_total"] == 1)

        eng.inject_fault("c1", "disconnect")
        assert wait_for(lambda: mode() == Mode.DEGRADED.value)
        eng.inject_fault("c2", "freeze")
        assert wait_for(lambda: mode() == Mode.FIXED.value)

        eng.inject_fault("c1", None)
        eng.inject_fault("c2", None)
        assert wait_for(lambda: mode() == Mode.ADAPTIVE.value)
        # pedestrian must have been served at some point
        assert wait_for(lambda: db.flush() or db.summary(__import__("datetime").datetime(2000, 1, 1))["ped_phases"] > 0,
                        timeout=40)
    finally:
        eng.stop()
    ev = [e["message"] for e in db.recent_events(100)]
    assert any("отказ" in m for m in ev) and any("восстановлена" in m for m in ev)
    assert eng.conflict_latched is None
    assert db.summary(__import__("datetime").datetime(2000, 1, 1))["vehicles"] > 0
