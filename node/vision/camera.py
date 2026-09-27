"""Приём видео с камеры (MJPEG/RTSP/файл) и контроль исправности по самому изображению.

Неисправности определяются так же, как на реальной камере:
- offline — нет кадров дольше OFFLINE_S (обрыв связи, камера не отвечает);
- black   — кадр почти чёрный (закрыт объектив, отказ матрицы);
- frozen  — кадр не меняется (зависание кодера);
- noise   — сильные помехи: почти все пиксели меняются от кадра к кадру.
Состояние меняется с задержкой (гистерезис), чтобы единичный плохой кадр не переключал режим.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

os.environ.setdefault("OPENCV_FFMPEG_READ_ATTEMPTS", "4096")

OFFLINE_S = 3.0
FAULT_HOLD_S = 2.0     # столько секунд признак должен держаться, чтобы объявить неисправность
RECOVER_S = 3.0        # столько секунд камера должна быть исправна, чтобы снова ей доверять

FAULT_TITLES = {"offline": "нет видеопотока", "black": "чёрный кадр", "frozen": "изображение зависло",
                "noise": "сильные помехи", None: "исправна"}


@dataclass
class Frame:
    seq: int
    ts: float
    img: np.ndarray


class CameraStream:
    def __init__(self, cam_id: str, url: str):
        self.id = cam_id
        self.url = url
        self.frame: Frame | None = None
        self.lock = threading.Lock()
        self.fault: str | None = "offline"
        self._candidate: str | None = "offline"
        self._candidate_since = time.monotonic()
        self._prev_small: np.ndarray | None = None
        self._same_since: float | None = None
        self._last_frame_mono = 0.0
        self.fps = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"cam-{cam_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Frame | None:
        with self.lock:
            return self.frame

    # ---------- чтение ----------
    def _run(self) -> None:
        backoff = 1.0
        seq = 0
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                cap.release()
                self._stop.wait(backoff)
                backoff = min(10.0, backoff * 1.5)
                continue
            backoff = 1.0
            t_fps, n_fps = time.monotonic(), 0
            while not self._stop.is_set():
                ok, img = cap.read()
                if not ok or img is None:
                    break
                seq += 1
                now = time.monotonic()
                self._last_frame_mono = now
                self._analyze(img, now)
                with self.lock:
                    self.frame = Frame(seq, time.time(), img)
                n_fps += 1
                if now - t_fps >= 2.0:
                    self.fps = n_fps / (now - t_fps)
                    t_fps, n_fps = now, 0
            cap.release()
            self._stop.wait(0.5)

    # ---------- исправность ----------
    def _analyze(self, img: np.ndarray, now: float) -> None:
        small = cv2.cvtColor(cv2.resize(img, (160, 90), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        body = small[8:]  # без строки с датой и временем
        sign = None
        if float(body.mean()) < 12:
            sign = "black"
        elif self._prev_small is not None:
            diff = cv2.absdiff(body, self._prev_small[8:])
            if float(np.median(diff)) > 8:
                sign = "noise"
            elif float(diff.max()) == 0:
                self._same_since = self._same_since or now
                if now - self._same_since > 1.5:
                    sign = "frozen"
            else:
                self._same_since = None
        self._prev_small = small
        self._set_candidate(sign, now)

    def _set_candidate(self, sign: str | None, now: float) -> None:
        if sign != self._candidate:
            self._candidate, self._candidate_since = sign, now
        hold = RECOVER_S if sign is None else FAULT_HOLD_S
        if sign != self.fault and now - self._candidate_since >= hold:
            self.fault = sign

    def health(self) -> str | None:
        """Текущая неисправность или None. Отсутствие кадров проверяется здесь, в потоке хаба."""
        now = time.monotonic()
        if now - self._last_frame_mono > OFFLINE_S:
            if self._candidate != "offline":
                self._candidate, self._candidate_since = "offline", now
            self.fault = "offline"
        return self.fault
