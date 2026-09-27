"""Сборка эмулятора: модель, 3D-сцена, камеры. Используется запуском (__main__) и замером (tools/bench.py)."""
from __future__ import annotations

import random
import time
import traceback
from collections import defaultdict


def configure_panda(window: bool) -> None:
    from panda3d.core import loadPrcFileData
    loadPrcFileData("", "\n".join([
        "window-type " + ("onscreen" if window else "offscreen"),
        "window-title Эмулятор участка Люблино",
        "win-size 1280 720",
        "audio-library-name null",
        "sync-video false",
        "framebuffer-multisample 1",
        "multisamples 4",
        "notify-level-display error",
        "notify-level-glgsg error",
    ]))


class Emulator:
    def __init__(self, s, window: bool = False, always_on: bool = True, warmup: float = 180.0, log=print):
        configure_panda(window)
        from direct.showbase.ShowBase import ShowBase

        from emulator.render import scene
        from emulator.render.cameras import CameraManager, FrameHub
        from emulator.render.labels import Labeler
        from emulator.render.visibility import Visibility
        from emulator.world.network import Network
        from emulator.world.sim import World

        self.s, self.scene = s, scene
        log("==> Строю дорожную сеть из OSM")
        self.net = Network.load()
        log("    " + self.net.describe())
        self.world = World(self.net, s)
        log(f"==> Прогрев модели: {warmup:.0f} с")
        self.world.warmup(warmup)
        self.world.update_poses()

        self.base = base = ShowBase()
        base.setBackgroundColor(0.72, 0.8, 0.88, 1)
        self.hub = FrameHub()
        self.cams = CameraManager(base, self.net, s.camera, self.hub, always_on=always_on)
        self.cams.labeler = Labeler(self.world, self.cams.dist, s.camera)
        # зона видимости камер: там полная детализация, вне её — упрощённая карта и скрытые модели
        self.vis = Visibility(self.cams.specs, s.camera, self.cams.dist.hfov_out)
        root = base.render.attachNewNode("static")
        scene.build_static(root, self.net, random.Random(s.seed), self.vis)
        self.lamps = scene.build_signals(base.render, self.net, scene.make_lamp_proto())
        self.agents = scene.AgentView(base.render, self.world, self.vis, window_cam=base.cam if window else None)
        scene.setup_lights(base.render)
        if s.camera.per_pixel_lighting:
            base.render.setShaderAuto()  # попиксельное освещение и блики на кузовах
        log(f"==> Камер: {len(self.cams.rigs)}, угол обзора {self.cams.dist.hfov_out:.0f}°, "
            f"кадр {s.camera.width}×{s.camera.height}, {s.camera.fps:g} кадр/с")
        if window:
            from emulator.render.orbit import OrbitCamera
            base.camLens.setNearFar(0.5, 5000)
            x0, y0, x1, y1 = self.net.bounds
            OrbitCamera(base, ((x0 + x1) / 2, (y0 + y1) / 2), yaw=20, pitch=50, dist=900,
                        objects=[(float(n.xy[0]), float(n.xy[1])) for n in self.net.objects.values()])
            log("    3D-окно: ЛКМ — поворот и наклон, ПКМ — сдвиг, колесо — масштаб, 1–0 — объекты, R — общий вид")
        self.acc = 0.0
        self.last = time.monotonic()
        self.perf = defaultdict(float)  # накопленное время по этапам, с
        self.perf_frames = 0

    def tick(self, lock, on_live=None) -> None:
        """Один кадр основного цикла: шаги модели, синхронизация сцены, постановка камер на рендер."""
        now = time.monotonic()
        self.acc += min(0.25, now - self.last)
        self.last = now
        s, w = self.s, self.world
        try:
            with lock:
                t0 = time.perf_counter()
                while self.acc >= s.sim_dt:
                    w.step(s.sim_dt)
                    self.acc -= s.sim_dt
                t1 = time.perf_counter()
                w.update_poses()
                self.agents.sync(w.t)
                self.scene.update_lamps(self.lamps, w.t)
                t2 = time.perf_counter()
                self.cams.update(now, w.t)
                t3 = time.perf_counter()
                if on_live:
                    on_live(now)
            self.perf["model"] += t1 - t0
            self.perf["scene_sync"] += t2 - t1
            self.perf["cameras"] += t3 - t2
            self.perf_frames += 1
        except Exception:  # ошибка в одном кадре не должна останавливать эмулятор
            self.acc = 0.0
            traceback.print_exc()
