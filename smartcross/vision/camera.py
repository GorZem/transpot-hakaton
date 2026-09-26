"""Camera worker: reads a video source, runs detection + zone analytics in its own thread.

Sources: video file (played in real time, optionally looped — used for demo/tests),
rtsp://... / http://... stream, or USB camera index ("0").

Health signals produced here: time of the last frame, frozen picture, dark picture.
The engine turns them into a stable healthy/unhealthy state (with hysteresis).
Faults can be injected ("disconnect", "freeze", "dark") to demo the emergency modes.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

from smartcross.config import CameraConfig, FallbackConfig, ZoneConfig
from smartcross.vision.zones import PEDESTRIAN, VEHICLE, Detection, ZoneAnalyzer, ZoneReport

log = logging.getLogger(__name__)

FAULTS = ("disconnect", "freeze", "dark")

ZONE_COLORS = {  # BGR
    "ped_wait": (0, 200, 255),
    "crosswalk": (255, 255, 255),
    "approach": (255, 120, 0),
    "count_line": (255, 0, 255),
}


@dataclass
class FrameResult:
    camera_id: str
    t: float
    zones: ZoneReport
    detections: int
    infer_ms: float


@dataclass
class CameraHealth:
    last_frame_t: float = 0.0
    frozen_since: float | None = None
    dark: bool = False
    error: str = ""
    fps: float = 0.0
    infer_ms: float = 0.0
    frames: int = 0


def draw_overlay(frame: np.ndarray, zones: list[ZoneConfig], dets: list[Detection], text: str) -> np.ndarray:
    h, w = frame.shape[:2]
    img = frame.copy()
    overlay = img.copy()
    for z in zones:
        pts = (np.array(z.points) * [w, h]).astype(np.int32)
        color = ZONE_COLORS.get(z.type, (200, 200, 200))
        if z.type == "count_line":
            cv2.line(img, tuple(pts[0]), tuple(pts[-1]), color, 3)
        else:
            cv2.fillPoly(overlay, [pts], color)
            cv2.polylines(img, [pts], True, color, 2)
        cv2.putText(img, z.id, tuple(pts[0] + [4, 18]), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    img = cv2.addWeighted(overlay, 0.18, img, 0.82, 0)
    for d in dets:
        x1, y1, x2, y2 = (int(v) for v in d.box)
        color = (0, 0, 255) if d.emergency else (80, 220, 80) if d.category == PEDESTRIAN else \
            (255, 160, 0) if d.category == VEHICLE else (200, 200, 0)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        label = f"{d.cls}{'' if d.track_id is None else ' #' + str(d.track_id)}{' EMERGENCY' if d.emergency else ''}"
        cv2.putText(img, label, (x1, max(y1 - 5, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        fx, fy = int((x1 + x2) / 2), y2
        cv2.circle(img, (fx, fy), 4, color, -1)
    cv2.rectangle(img, (0, 0), (w, 30), (0, 0, 0), -1)
    cv2.putText(img, text, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    return img


class CameraWorker(threading.Thread):
    def __init__(self, cam: CameraConfig, fallback: FallbackConfig, detector_factory: Callable[[], Callable],
                 results: "queue.Queue[FrameResult]", clock: Callable[[], float] = time.monotonic):
        super().__init__(name=f"cam-{cam.id}", daemon=True)
        self.cam = cam
        self.fb = fallback
        self.detector_factory = detector_factory
        self.results = results
        self.clock = clock
        self.health = CameraHealth(last_frame_t=clock())
        self.fault: str | None = None
        self.analyzer = ZoneAnalyzer(cam.zones)
        self._zones_lock = threading.Lock()
        self._stop = threading.Event()
        self._jpeg: bytes | None = None
        self._raw_jpeg: bytes | None = None
        self._jpeg_lock = threading.Lock()
        self._prev_small: np.ndarray | None = None

    # ----------------------------------------------------------------- API
    def stop(self) -> None:
        self._stop.set()

    def set_zones(self, zones: list[ZoneConfig]) -> None:
        with self._zones_lock:
            self.cam = self.cam.model_copy(update={"zones": zones})
            self.analyzer = ZoneAnalyzer(zones)

    def jpeg(self, raw: bool = False) -> bytes | None:
        with self._jpeg_lock:
            return self._raw_jpeg if raw else self._jpeg

    # ------------------------------------------------------------ internals
    def _open(self) -> tuple[cv2.VideoCapture, bool]:
        src = self.cam.source
        is_file = not (src.isdigit() or "://" in src)
        cap = cv2.VideoCapture(int(src) if src.isdigit() else src)
        if not cap.isOpened():
            raise IOError(f"не удалось открыть источник {src}")
        return cap, is_file

    def run(self) -> None:
        try:
            detector = self.detector_factory()
        except Exception as e:  # noqa: BLE001
            log.exception("detector init failed")
            self.health.error = f"детектор: {e}"
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                cap, is_file = self._open()
                self.health.error = ""
                backoff = 1.0
                self._loop(cap, is_file, detector)
                cap.release()
            except Exception as e:  # noqa: BLE001
                self.health.error = str(e)
                log.warning("camera %s: %s; reconnect in %.0fs", self.cam.id, e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    def _loop(self, cap: cv2.VideoCapture, is_file: bool, detector: Callable) -> None:
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        period = max(1.0 / self.cam.fps, 1.0 / src_fps if is_file else 0.0)
        t_start = self.clock()
        pos = 0  # frames consumed from a file
        frozen_frame: np.ndarray | None = None
        last_proc = 0.0
        fps_ema = 0.0
        while not self._stop.is_set():
            if self.fault == "disconnect":
                self._stop.wait(0.2)
                continue
            if is_file:
                # real-time playback: skip frames to where the wall clock says we should be
                target = int((self.clock() - t_start) * src_fps)
                ok, frame = True, None
                while pos <= target and ok:
                    ok = cap.grab()
                    pos += 1
                if ok:
                    ok, frame = cap.retrieve()
                if not ok:
                    if not self.cam.loop:
                        raise IOError("видеофайл закончился")
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    pos, t_start = 0, self.clock()
                    continue
            else:
                ok, frame = cap.read()
                if not ok:
                    raise IOError("поток прерван")
                if self.clock() - last_proc < period:
                    continue

            if self.fault == "freeze":
                frozen_frame = frame if frozen_frame is None else frozen_frame
                frame = frozen_frame
            else:
                frozen_frame = None
            if self.fault == "dark":
                frame = np.zeros_like(frame)

            now = self.clock()
            self._check_picture(frame, now)
            self.health.last_frame_t = now
            if last_proc:
                fps_ema = 0.8 * fps_ema + 0.2 / max(now - last_proc, 1e-3) if fps_ema else 1 / max(now - last_proc, 1e-3)
            self.health.fps = round(fps_ema, 1)
            last_proc = now

            t0 = time.perf_counter()
            dets = detector(frame)
            with self._zones_lock:
                h, w = frame.shape[:2]
                rep = self.analyzer.analyze(dets, w, h)
                zones = list(self.cam.zones)
            infer_ms = (time.perf_counter() - t0) * 1000
            self.health.infer_ms = round(0.8 * self.health.infer_ms + 0.2 * infer_ms, 1)
            self.health.frames += 1
            self._put(FrameResult(self.cam.id, now, rep, len(dets), infer_ms))

            text = (f"{self.cam.name or self.cam.id} | люди в ожидании: {sum(rep.ped_waiting.values())} "
                    f"| на переходе: {rep.ped_on_crossing} | ТС в зоне: {sum(rep.veh_in_approach.values())}"
                    f" | {self.health.infer_ms:.0f} мс")
            self._encode(frame, draw_overlay(frame, zones, dets, text))

            if is_file:
                sleep = period - (self.clock() - now)
                if sleep > 0:
                    self._stop.wait(sleep)

    def _check_picture(self, frame: np.ndarray, now: float) -> None:
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 36), interpolation=cv2.INTER_AREA)
        self.health.dark = float(small.mean()) < self.fb.dark_threshold
        if self._prev_small is not None and float(cv2.absdiff(small, self._prev_small).mean()) < 0.05:
            if self.health.frozen_since is None:
                self.health.frozen_since = now
        else:
            self.health.frozen_since = None
        self._prev_small = small

    def _put(self, r: FrameResult) -> None:
        try:
            self.results.put_nowait(r)
        except queue.Full:
            pass

    def _encode(self, raw: np.ndarray, annotated: np.ndarray) -> None:
        def enc(img):
            h, w = img.shape[:2]
            if w > 960:
                img = cv2.resize(img, (960, int(h * 960 / w)))
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            return buf.tobytes() if ok else None
        a, r = enc(annotated), enc(raw)
        with self._jpeg_lock:
            self._jpeg, self._raw_jpeg = a, r
