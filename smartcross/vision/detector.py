"""YOLO detection + tracking (Ultralytics) and emergency vehicle heuristic."""
from __future__ import annotations

import logging
from collections import defaultdict, deque

import cv2
import numpy as np

from smartcross.config import DetectorConfig
from smartcross.vision.zones import COCO_CATEGORY, VEHICLE, Detection

log = logging.getLogger(__name__)


class EmergencyLightDetector:
    """Detects flashing red/blue beacons on top of tracked vehicles.

    COCO has no "ambulance"/"police" class, so without a custom-trained model we look
    at the upper third of a vehicle box: a beacon produces bright saturated red/blue
    pixels whose share changes strongly between frames. A vehicle is flagged if that
    share is large and flickering for several consecutive frames.
    For production, a YOLO model fine-tuned on emergency vehicles is plugged in via
    `detector.emergency_classes` (see docs).
    """

    def __init__(self, history: int = 12, min_peak: float = 0.02, min_flicker: float = 0.6, confirm: int = 4):
        self.hist: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=history))
        self.hits: dict[int, int] = defaultdict(int)
        self.min_peak, self.min_flicker, self.confirm = min_peak, min_flicker, confirm

    @staticmethod
    def beacon_ratio(frame: np.ndarray, box) -> float:
        x1, y1, x2, y2 = (int(v) for v in box)
        h = y2 - y1
        roi = frame[max(y1, 0):max(y1 + h // 3, y1 + 1), max(x1, 0):max(x2, x1 + 1)]
        if roi.size == 0:
            return 0.0
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        bright = (sat > 150) & (val > 200)
        red = bright & ((hue < 8) | (hue > 170))
        blue = bright & (hue > 100) & (hue < 130)
        return float((red | blue).mean())

    def update(self, frame: np.ndarray, track_id: int, box) -> bool:
        h = self.hist[track_id]
        h.append(self.beacon_ratio(frame, box))
        if len(h) >= 4:
            arr = np.array(h)
            peak = arr.max()
            flicker = arr.std() / (arr.mean() + 1e-6)
            if peak >= self.min_peak and flicker >= self.min_flicker:
                self.hits[track_id] += 1
            else:
                self.hits[track_id] = max(0, self.hits[track_id] - 1)
        return self.hits[track_id] >= self.confirm

    def forget(self, alive: set[int]) -> None:
        for tid in list(self.hist):
            if tid not in alive:
                self.hist.pop(tid, None)
                self.hits.pop(tid, None)


class YoloDetector:
    def __init__(self, cfg: DetectorConfig):
        import torch
        from ultralytics import YOLO

        torch.set_num_threads(cfg.threads)
        self.cfg = cfg
        self.model = YOLO(cfg.model)
        names = self.model.names
        self.names = names if isinstance(names, dict) else dict(enumerate(names))
        wanted = set(COCO_CATEGORY) | set(cfg.emergency_classes)
        self.class_ids = [i for i, n in self.names.items() if n in wanted]
        self.emergency = EmergencyLightDetector() if cfg.emergency_light_detection else None
        self._n = 0

    def __call__(self, frame: np.ndarray) -> list[Detection]:
        res = self.model.track(frame, persist=True, imgsz=self.cfg.imgsz, conf=self.cfg.conf,
                               classes=self.class_ids or None, device=self.cfg.device,
                               tracker=self.cfg.tracker, verbose=False)[0]
        out: list[Detection] = []
        if res.boxes is None or len(res.boxes) == 0:
            return out
        boxes = res.boxes.xyxy.cpu().numpy()
        cls = res.boxes.cls.cpu().numpy().astype(int)
        conf = res.boxes.conf.cpu().numpy()
        ids = res.boxes.id.cpu().numpy().astype(int) if res.boxes.id is not None else [None] * len(boxes)
        alive = set()
        for b, c, p, tid in zip(boxes, cls, conf, ids):
            name = self.names.get(int(c), str(c))
            is_emerg_cls = name in self.cfg.emergency_classes
            category = VEHICLE if is_emerg_cls else COCO_CATEGORY.get(name)
            if category is None:
                continue
            d = Detection(None if tid is None else int(tid), name, category, tuple(float(v) for v in b), float(p))
            if category == VEHICLE:
                d.emergency = is_emerg_cls
                if self.emergency is not None and d.track_id is not None:
                    alive.add(d.track_id)
                    d.emergency |= self.emergency.update(frame, d.track_id, b)
            out.append(d)
        self._n += 1
        if self.emergency is not None and self._n % 100 == 0:
            self.emergency.forget(alive)
        return out
