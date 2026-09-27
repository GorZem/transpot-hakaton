"""Связь с дорожным контроллером объекта (выход на светофор).

Протокол контроллера (HTTP):
- GET  {path}            — состояние: режим (local / remote / flash) и группы сигналов;
- PUT  {path}/signals    — задать состояния групп, контроллер переходит в remote;
- POST {path}/heartbeat  — подтверждение связи; без команд и heartbeat контроллер возвращается к своей программе;
- POST {path}/release    — вернуть контроллер к своей программе.

Подхват управления: пока система не управляет, контроллер работает по своей программе.
Система ждёт устойчивой фазы (зелёный одному направлению, остальным красный) и начинает
управление с неё, чтобы не обрывать такты на середине.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("equipment")

# Названия состояний у контроллера объекта, если отличаются от наших.
TO_DEVICE = {"yellow_flash": "flash_yellow"}
FROM_DEVICE = {v: k for k, v in TO_DEVICE.items()}


class EquipmentLink:
    def __init__(self, base_url: str, path: str, heartbeat_s: float = 5.0, timeout_s: float = 2.0):
        self.url = base_url.rstrip("/") + path
        self.heartbeat_s = heartbeat_s
        self.timeout = timeout_s
        self.connected = False
        self.device_mode: str | None = None       # local | remote | flash
        self.device_groups: dict[str, str] = {}
        self.last_error: str | None = None
        self.rejected: str | None = None          # последний отказ контроллера исполнить команду
        self.active = False                       # система управляет объектом (иначе heartbeat не шлём)
        self._released = False
        self._q: queue.Queue = queue.Queue(maxsize=8)
        self._last_sent: dict[str, str] | None = None
        self._last_io = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"eq-{path}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def send(self, signals: dict[str, str]) -> None:
        """Отправить новое состояние групп (асинхронно, без блокировки такта контроллера)."""
        sig = {g: TO_DEVICE.get(str(getattr(s, "value", s)), str(getattr(s, "value", s))) for g, s in signals.items()}
        self._released = False
        if sig == self._last_sent:
            return
        self._last_sent = sig
        try:
            self._q.put_nowait(("signals", sig))
        except queue.Full:
            pass

    def _request(self, method: str, path: str = "", body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def _absorb(self, snap: dict) -> None:
        sig = snap.get("signal", snap)
        self.device_mode = sig.get("mode")
        self.device_groups = {g: FROM_DEVICE.get(v["state"], v["state"]) for g, v in sig.get("groups", {}).items()}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                kind, payload = self._q.get(timeout=0.5)
            except queue.Empty:
                kind, payload = None, None
            try:
                if kind == "signals":
                    # из очереди берём только самое свежее состояние
                    while not self._q.empty():
                        kind, payload = self._q.get_nowait()
                    try:
                        self._absorb(self._request("PUT", "/signals", {"groups": payload}))
                        self.rejected = None
                    except urllib.error.HTTPError as e:
                        detail = e.read().decode("utf-8", "replace")
                        self.rejected = detail[:300]
                        log.warning("контроллер отклонил команду: %s", detail)
                        self._absorb(self._request("GET"))
                    self._last_io = time.monotonic()
                elif time.monotonic() - self._last_io >= (self.heartbeat_s if self.active else 0.5):
                    if self.active and self.device_mode == "remote":
                        snap = self._request("POST", "/heartbeat")
                    elif not self.active and self.device_mode == "remote" and not self._released:
                        # осталось управление от прошлого сеанса: вернуть объект к своей программе
                        snap = self._request("POST", "/release")
                        self._released = True
                    else:
                        snap = self._request("GET")
                    self._absorb(snap)
                    self._last_io = time.monotonic()
                self.connected, self.last_error = True, None
            except Exception as e:
                self.connected = False
                self.last_error = str(e)[:200]
                self._last_sent = None  # после восстановления связи отправить состояние заново
                self._stop.wait(1.0)
