"""Детектор YOLO: один на все камеры, кадры обрабатываются пакетом на видеокарте.

Классы COCO: человек, велосипед, машина, мотоцикл, автобус, грузовик. Спецтранспорт отдельного
класса в COCO нет, он определяется по проблесковому маячку (синие и красные яркие пиксели
в верхней части рамки машины) — эвристика до дообучения модели.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

log = logging.getLogger("detector")

PERSON, VEHICLE = "person", "vehicle"
COCO = {0: PERSON, 1: PERSON, 2: VEHICLE, 3: VEHICLE, 5: VEHICLE, 7: VEHICLE}  # велосипедист — как пешеход у перехода
KIND_NAMES = {0: "пешеход", 1: "велосипед", 2: "легковой", 3: "мотоцикл", 5: "автобус", 7: "грузовик"}


@dataclass
class Detection:
    kind: str             # person | vehicle
    cls: int
    conf: float
    box: tuple[float, float, float, float]
    beacon: str | None = None  # цвет проблескового маячка на кадре: blue | red

    @property
    def foot(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        return (x1 + x2) / 2, y2


def has_beacon(img: np.ndarray, box) -> str | None:
    """Цвет яркого маячка в верхней части рамки: 'blue', 'red' или None.
    Один кадр не доказывает спецтранспорт (синий кузов тоже синий): маячок мигает синим и красным
    по очереди, поэтому решение принимает трек по смене цветов на нескольких кадрах."""
    x1, y1, x2, y2 = (int(v) for v in box)
    h = y2 - y1
    if h < 12 or x2 - x1 < 12:
        return None
    top = img[max(0, y1 - h // 6): y1 + h // 3, max(0, x1): x2]
    if top.size == 0:
        return None
    hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
    bright = (hsv[..., 1] > 150) & (hsv[..., 2] > 170)
    n = top.shape[0] * top.shape[1]
    blue = float((bright & (hsv[..., 0] > 100) & (hsv[..., 0] < 130)).sum()) / n
    red = float((bright & ((hsv[..., 0] < 8) | (hsv[..., 0] > 172))).sum()) / n
    if max(blue, red) < 0.01:
        return None
    return "blue" if blue >= red else "red"


class Detector:
    def __init__(self, model: str = "yolo11s.pt", imgsz: int = 960, conf: float = 0.2, device: str | int = 0):
        self.model_name, self.imgsz, self.conf, self.device = model, imgsz, conf, device
        self.model = None
        self.ms_per_image = 0.0

    def load(self) -> None:
        from ultralytics import YOLO
        self.model = YOLO(self.model_name)
        self.model.predict(np.zeros((self.imgsz * 9 // 16, self.imgsz, 3), np.uint8), imgsz=self.imgsz,
                           device=self.device, verbose=False)  # прогрев

    def detect(self, images: list[np.ndarray]) -> list[list[Detection]]:
        if not images:
            return []
        t = time.perf_counter()
        res = self.model.predict(images, imgsz=self.imgsz, conf=self.conf, device=self.device, verbose=False,
                                 classes=list(COCO))
        self.ms_per_image = (time.perf_counter() - t) * 1000 / len(images)
        out = []
        for img, r in zip(images, res):
            dets = []
            b = r.boxes
            for xyxy, c, cf in zip(b.xyxy.cpu().numpy(), b.cls.cpu().numpy().astype(int), b.conf.cpu().numpy()):
                kind = COCO[int(c)]
                d = Detection(kind, int(c), float(cf), tuple(float(v) for v in xyxy))
                if kind == VEHICLE:
                    d.beacon = has_beacon(img, xyxy)
                dets.append(d)
            out.append(dets)
        return out


class DetectorWorker:
    """Фоновый поток: забирает свежие кадры всех камер и прогоняет их пакетом."""

    def __init__(self, detector: Detector, fps_per_camera: float = 3.0):
        self.det = detector
        self.period = 1.0 / fps_per_camera
        self.sources: dict[str, Callable] = {}         # камера -> функция, возвращающая свежий кадр или None
        self.sinks: dict[str, Callable] = {}           # камера -> куда отдать (кадр, детекции)
        self.last: dict[str, tuple[int, float, list[Detection]]] = {}  # камера -> (seq, ts, детекции)
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.error: str | None = None
        self.paused = False            # остановить распознавание (проверка аварийного режима «компьютерное зрение не работает»)
        self.last_cycle = time.monotonic()  # когда цикл распознавания последний раз отработал
        self._thread = threading.Thread(target=self._run, name="detector", daemon=True)

    def add(self, cam_id: str, source: Callable, sink: Callable) -> None:
        self.sources[cam_id] = source
        self.sinks[cam_id] = sink

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            self.det.load()
        except Exception as e:
            self.error = f"детектор не загрузился: {e}"
            log.exception(self.error)
            return
        self.ready.set()
        while not self._stop.is_set():
            t0 = time.monotonic()
            if self.paused:
                self._stop.wait(0.2)
                continue
            batch = []
            for cid, src in self.sources.items():
                fr = src()
                if fr is not None and self.last.get(cid, (-1,))[0] != fr.seq:
                    batch.append((cid, fr))
            if batch:
                try:
                    results = self.det.detect([fr.img for _, fr in batch])
                except Exception:
                    log.exception("ошибка детектора")
                    self._stop.wait(0.5)
                    continue  # результатов нет: если сбой не пройдёт, объекты перейдут в фиксированный план
                for (cid, fr), dets in zip(batch, results):
                    self.last[cid] = (fr.seq, fr.ts, dets)
                    self.sinks[cid](fr, dets)
            self.last_cycle = time.monotonic()
            self._stop.wait(max(0.01, self.period - (time.monotonic() - t0)))
