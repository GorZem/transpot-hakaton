"""Камеры объектов: расстановка на опорах, рендер в текстуру, широкоугольное искажение, JPEG, раздача кадров.

Каждая камера рендерится прямолинейной проекцией с запасом по углу, затем кадр перекладывается
(cv2.remap) в модель широкоугольного объектива OpenCV: K = [[f,0,W/2],[0,f,H/2],[0,0,1]], D = [k1,k2,0,0,0].
Эти K и D отдаются в API, поэтому система может снять искажение cv2.undistort.
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np
from panda3d.core import (FrameBufferProperties, GraphicsOutput, GraphicsPipe, NodePath, PerspectiveLens, Texture,
                          WindowProperties)

from emulator.config import CameraSettings
from emulator.world.geom import heading_deg, right_normal, unit
from emulator.world.network import Network, Node

FAULTS = ("black", "freeze", "offline", "noise", "fog", "covered")

# Поворотное устройство камеры: смещение от исходного положения при монтаже.
PAN_LIMIT_DEG = 90.0            # не дальше 90° влево и вправо от исходного направления
TILT_ABS_DEG = (5.0, 75.0)      # наклон оси вниз от горизонта: от почти горизонта до почти отвесно
PAN_SPEED_DPS = 30.0
TILT_SPEED_DPS = 20.0


@dataclass
class CameraSpec:
    id: str
    object_id: str
    name: str
    pos: np.ndarray        # x, y, z
    target: np.ndarray     # x, y, z — куда смотрит ось сейчас
    home: np.ndarray | None = None  # куда смотрела ось при монтаже

    def __post_init__(self):
        if self.home is None:
            self.home = self.target.copy()


def place_cameras(net: Network, height: float) -> list[CameraSpec]:
    """Две камеры на объект: переход — по разные стороны дороги, смотрят в разные стороны;
    крестовой — противоположные углы по диагонали; Т-образный — напротив примыкания и на его углу."""
    specs: list[CameraSpec] = []

    def add(node: Node, k: int, p, t, label: str):
        oid = node.object_id
        specs.append(CameraSpec(f"{oid}-cam{k}", oid, f"Камера {k}: {label}",
                                np.array([p[0], p[1], height]), np.array([t[0], t[1], 0.0])))

    for oid, n in net.objects.items():
        c = n.xy
        if n.kind == "crossing":
            arm = next((a for a in n.arms if a.at_start), n.arms[0])
            u = unit(arm.dir_away())
            nr = right_normal(u)
            e = arm.edge
            hr, hl = (e.half_right, e.half_left) if arm.at_start else (e.half_left, e.half_right)
            add(n, 1, c + nr * (hr + 1.6) - u * 2.0, c - u * 10 - nr * (hl + 3.0), "подход с одной стороны")
            add(n, 2, c - nr * (hl + 1.6) + u * 2.0, c + u * 10 + nr * (hr + 3.0), "подход с другой стороны")
        elif n.degree >= 4:
            arms = sorted(n.arms, key=lambda a: a.bearing)
            a1 = next(a for a in arms if a.axis == "A")
            i = arms.index(a1)
            b1 = arms[(i + 1) % len(arms)]
            d = unit(unit(a1.dir_away()) + unit(b1.dir_away()))
            r = n.radius * 1.3 + 2.5
            add(n, 1, c + d * r, c - d * (n.radius * 0.8), "угол перекрёстка, на противоположный угол")
            add(n, 2, c - d * r, c + d * (n.radius * 0.8), "противоположный угол")
        else:
            stem = next((a for a in n.arms if a.axis == "B"), n.arms[-1])
            mains = [a for a in n.arms if a is not stem]
            ds = unit(stem.dir_away())
            half_main = max(a.edge.width for a in mains) / 2
            add(n, 1, c - ds * (half_main + 3.0), c + ds * 12, "напротив примыкания")
            dm = unit(mains[0].dir_away())
            dcorner = unit(ds + dm)
            dother = unit(unit(mains[-1].dir_away()) - ds)
            add(n, 2, c + dcorner * (n.radius * 1.3 + 2.5), c + dother * (n.radius + 4), "угол примыкания")
    return specs


class Distortion:
    """Карты перекладки прямолинейного рендера в широкоугольный кадр."""

    def __init__(self, cs: CameraSettings):
        W, H, f = cs.width, cs.height, cs.focal_px
        self.K = np.array([[f, 0, W / 2], [0, f, H / 2], [0, 0, 1]], dtype=np.float64)
        self.D = np.array([cs.k1, cs.k2, 0, 0, 0], dtype=np.float64)
        self.k1, self.k2 = cs.k1, cs.k2
        xs, ys = np.meshgrid(np.arange(W, dtype=np.float64), np.arange(H, dtype=np.float64))
        und = self.undistort_norm((xs - W / 2) / f, (ys - H / 2) / f)
        mx, my = float(np.abs(und[..., 0]).max()), float(np.abs(und[..., 1]).max())
        self.fr = f * cs.render_scale  # масштаб центра рендера
        self.Wr = int(math.ceil(mx * self.fr)) * 2 + 4
        self.Hr = int(math.ceil(my * self.fr)) * 2 + 4
        self.hfov_r = math.degrees(2 * math.atan(self.Wr / 2 / self.fr))
        self.vfov_r = math.degrees(2 * math.atan(self.Hr / 2 / self.fr))
        self.map_x = (und[..., 0] * self.fr + self.Wr / 2).astype(np.float32)
        # текстура OpenGL перевёрнута по вертикали
        self.map_y = ((self.Hr - 1) - (und[..., 1] * self.fr + self.Hr / 2)).astype(np.float32)
        edge = self.undistort_norm(np.array([(W / 2) / f]), np.array([0.0]))
        self.hfov_out = math.degrees(2 * math.atan(abs(float(edge[0, 0]))))

    def undistort_norm(self, xd: np.ndarray, yd: np.ndarray) -> np.ndarray:
        """Обратное радиальное искажение r_d = r_u (1 + k1 r_u^2 + k2 r_u^4) методом Ньютона."""
        rd = np.hypot(xd, yd)
        ru = rd.copy()
        for _ in range(30):
            r2 = ru * ru
            f = ru * (1 + self.k1 * r2 + self.k2 * r2 * r2) - rd
            df = 1 + 3 * self.k1 * r2 + 5 * self.k2 * r2 * r2
            ru = ru - f / df
        scale = np.where(rd > 1e-12, ru / np.maximum(rd, 1e-12), 1.0)
        return np.stack([xd * scale, yd * scale], axis=-1)


@dataclass(eq=False)
class Rig:
    spec: CameraSpec
    buffer: object
    tex: Texture
    cam_np: NodePath
    last_capture: float = 0.0
    fault: str | None = None
    rendering: bool = False
    busy: bool = False               # предыдущий кадр ещё обрабатывается в фоновом потоке
    frame_no: int = 0                # номер кадра цикла, в котором камера поставлена на рендер
    labels: list | None = None      # разметка кадра, который сейчас рендерится
    labels_meta: dict | None = None
    home_az: float = 0.0             # исходное направление оси: азимут и наклон вниз, °
    home_tilt: float = 0.0
    pan: float = 0.0                 # текущее смещение от исходного, °
    tilt: float = 0.0
    pan_goal: float = 0.0            # заданное смещение: камера поворачивается к нему плавно
    tilt_goal: float = 0.0
    moved_at: float = 0.0            # время последнего движения (time.monotonic)

    @property
    def moving(self) -> bool:
        return abs(self.pan - self.pan_goal) > 1e-3 or abs(self.tilt - self.tilt_goal) > 1e-3

    def tilt_range(self) -> tuple[float, float]:
        return TILT_ABS_DEG[0] - self.home_tilt, TILT_ABS_DEG[1] - self.home_tilt

    def aim(self, pan: float | None = None, tilt: float | None = None) -> None:
        """Задать положение (смещение от исходного); за пределами хода — упор."""
        if pan is not None:
            self.pan_goal = max(-PAN_LIMIT_DEG, min(PAN_LIMIT_DEG, float(pan)))
        if tilt is not None:
            lo, hi = self.tilt_range()
            self.tilt_goal = max(lo, min(hi, float(tilt)))


class FrameHub:
    """Последние кадры камер для раздачи по HTTP (потокобезопасно)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frames: dict[str, tuple[int, bytes, float]] = {}
        self.viewers: dict[str, int] = defaultdict(int)
        self.requested: dict[str, float] = {}
        self.offline: set[str] = set()
        self.want_labels: dict[str, float] = {}
        self.labeled: dict[str, tuple[int, bytes, float, list, dict]] = {}

    def publish(self, cid: str, jpeg: bytes, labels: list | None = None, meta: dict | None = None) -> int:
        with self.lock:
            seq = self.frames.get(cid, (0, b"", 0.0))[0] + 1
            ts = time.time()
            self.frames[cid] = (seq, jpeg, ts)
            if labels is not None:
                self.labeled[cid] = (seq, jpeg, ts, labels, meta or {})
            return seq

    def request_labels(self, cid: str) -> None:
        with self.lock:
            self.want_labels[cid] = time.monotonic() + 5.0
            self.requested[cid] = time.monotonic()

    def labels_wanted(self, cid: str) -> bool:
        with self.lock:
            return self.want_labels.get(cid, 0.0) > time.monotonic()

    def get_labeled(self, cid: str):
        with self.lock:
            return self.labeled.get(cid)

    def get(self, cid: str) -> tuple[int, bytes, float] | None:
        with self.lock:
            return self.frames.get(cid)

    def touch(self, cid: str) -> None:
        with self.lock:
            self.requested[cid] = time.monotonic()

    def viewer(self, cid: str, delta: int) -> None:
        with self.lock:
            self.viewers[cid] = max(0, self.viewers[cid] + delta)

    def wanted(self, cid: str, idle_s: float) -> bool:
        with self.lock:
            return self.viewers[cid] > 0 or time.monotonic() - self.requested.get(cid, -1e9) < idle_s


class CameraManager:
    def __init__(self, base, net: Network, cs: CameraSettings, hub: FrameHub, always_on: bool = False,
                 threaded: bool = False):
        self.base, self.net, self.cs, self.hub = base, net, cs, hub
        # в многопоточном конвейере Panda3D кадр готов в памяти на один кадр цикла позже
        self.lag = 2 if threaded else 1
        self.always_on = always_on
        self.dist = Distortion(cs)
        self.specs = place_cameras(net, cs.mount_height_m)
        self.rigs: dict[str, Rig] = {}
        self.pending: list[Rig] = []
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="cam")
        self.frozen: dict[str, bytes] = {}
        self.labeler = None  # emulator.render.labels.Labeler, задаётся при запуске
        for sp in self.specs:
            self.rigs[sp.id] = self._make_rig(sp, self.dist.Wr, self.dist.Hr, self.dist.hfov_r, self.dist.vfov_r)
        # обзорная камера для демонстрации (без искажения)
        self.overview = self._make_rig(CameraSpec("overview", "", "Обзор", np.zeros(3), np.zeros(3)),
                                       cs.width, cs.height, 60.0,
                                       math.degrees(2 * math.atan(math.tan(math.radians(30)) * cs.height / cs.width)))
        self.overview_target: str | None = None
        self.overview_yaw = 0.0
        self.set_overview(next(iter(net.objects)))
        self._build_poles()

    def _make_rig(self, sp: CameraSpec, W: int, H: int, hfov: float, vfov: float) -> Rig:
        fb = FrameBufferProperties()
        fb.setRgbColor(True)
        fb.setDepthBits(24)
        fb.setMultisamples(self.cs.msaa)
        buf = self.base.graphicsEngine.makeOutput(self.base.pipe, f"buf_{sp.id}", -2, fb, WindowProperties.size(W, H),
                                                  GraphicsPipe.BFRefuseWindow, self.base.win.getGsg(), self.base.win)
        if buf is None:  # без мультисэмплинга
            fb.setMultisamples(0)
            buf = self.base.graphicsEngine.makeOutput(self.base.pipe, f"buf_{sp.id}", -2, fb,
                                                      WindowProperties.size(W, H), GraphicsPipe.BFRefuseWindow,
                                                      self.base.win.getGsg(), self.base.win)
        tex = Texture()
        buf.addRenderTexture(tex, GraphicsOutput.RTMCopyRam)
        buf.setClearColor((0.72, 0.8, 0.88, 1))
        lens = PerspectiveLens()
        lens.setFov(hfov, vfov)
        lens.setNearFar(0.3, 420)  # дальше дома в кадре — несколько пикселей
        cam = self.base.makeCamera(buf, lens=lens)
        # makeCamera цепляет камеру к base.camera; камера объекта закреплена в мире,
        # иначе она двигается вместе с камерой 3D-окна
        cam.reparentTo(self.base.render)
        cam.setPos(*sp.pos)
        cam.lookAt(*sp.target)
        buf.setActive(False)
        rig = Rig(sp, buf, tex, cam)
        v = sp.target - sp.pos
        if np.hypot(v[0], v[1]) > 1e-6:
            rig.home_az = heading_deg(v[:2])
            rig.home_tilt = math.degrees(math.atan2(-v[2], math.hypot(v[0], v[1])))
        return rig

    def _build_poles(self) -> None:
        from emulator.render.scene import Mesh
        m = Mesh("camera_poles")
        for sp in self.specs:
            x, y, z = sp.pos
            m.cylinder(x, y, 0, 0.09, z - 0.1, (0.35, 0.37, 0.4), seg=8)
            d = unit(sp.target[:2] - sp.pos[:2])
            m.box(x + d[0] * 0.2, y + d[1] * 0.2, z - 0.1, 0.22, 0.22, 0.2, (0.85, 0.86, 0.88))
        np_ = m.node()
        np_.reparentTo(self.base.render)
        np_.flattenStrong()

    # ------------------------------------------------------------ обзор
    def set_overview(self, object_id: str) -> None:
        if object_id in self.net.objects:
            self.overview_target = object_id

    def _aim_overview(self, t: float) -> None:
        n = self.net.objects[self.overview_target]
        a = math.radians(t * 4.0)
        p = (n.xy[0] + 55 * math.cos(a), n.xy[1] + 55 * math.sin(a), 38)
        self.overview.cam_np.setPos(*p)
        self.overview.cam_np.lookAt(n.xy[0], n.xy[1], 0)

    # ------------------------------------------------------------ каждый кадр
    def _step_ptz(self, now: float) -> None:
        dt = min(0.5, max(0.0, now - getattr(self, "_ptz_t", now)))
        self._ptz_t = now
        for rig in self.rigs.values():
            if not rig.moving:
                continue
            dp, dtl = rig.pan_goal - rig.pan, rig.tilt_goal - rig.tilt
            rig.pan += max(-PAN_SPEED_DPS * dt, min(PAN_SPEED_DPS * dt, dp))
            rig.tilt += max(-TILT_SPEED_DPS * dt, min(TILT_SPEED_DPS * dt, dtl))
            rig.moved_at = time.monotonic()
            sp = rig.spec
            az = math.radians(rig.home_az + rig.pan)
            tl = math.radians(rig.home_tilt + rig.tilt)
            f = np.array([math.sin(az) * math.cos(tl), math.cos(az) * math.cos(tl), -math.sin(tl)])
            sp.target = sp.pos + f * (sp.pos[2] / max(math.sin(tl), 1e-3))
            rig.cam_np.lookAt(*(sp.pos + f))

    def update(self, now: float, sim_t: float) -> None:
        self._step_ptz(now)
        stamp = datetime.now()
        from panda3d.core import ClockObject
        fc = ClockObject.getGlobalClock().getFrameCount()
        still = []
        for rig in self.pending:
            if fc - rig.frame_no >= 1:
                rig.buffer.setActive(False)  # один раз отрендерили — хватит
            if fc - rig.frame_no < self.lag:
                still.append(rig)
                continue
            rig.rendering = False
            raw = rig.tex.getRamImage()  # ссылка на кадр в памяти, без копирования в основном потоке
            if not raw:
                continue
            rig.busy = True
            self.pool.submit(self._process, rig, raw, (rig.tex.getYSize(), rig.tex.getXSize(), rig.tex.getNumComponents()),
                             stamp, rig.labels, rig.labels_meta)
            rig.labels = rig.labels_meta = None
        self.pending = still
        period = 1.0 / self.cs.fps
        for rig in list(self.rigs.values()) + [self.overview]:
            cid = rig.spec.id
            if not (self.always_on and cid != "overview") and not self.hub.wanted(cid, self.cs.idle_stop_s):
                continue
            if now - rig.last_capture < period or rig.busy or rig.rendering:
                continue
            # расписание без накопления ошибки: следующий кадр через период от запланированного момента
            rig.last_capture = now if now - rig.last_capture > 2 * period else rig.last_capture + period
            if rig.fault == "offline":
                continue
            if rig.fault == "freeze" and cid in self.frozen:
                self.hub.publish(cid, self.frozen[cid])
                continue
            if cid == "overview":
                self._aim_overview(sim_t)
            elif self.labeler is not None and self.hub.labels_wanted(cid):
                # разметка из того же состояния сцены, которое сейчас уйдёт в рендер
                m = rig.cam_np.getMat(self.base.render)
                mat = np.array([[m.getCell(r, c) for c in range(4)] for r in range(4)])
                rig.labels = self.labeler.label(mat, np.asarray(rig.spec.pos, dtype=float))
                rig.labels_meta = {"sim_time_s": round(sim_t, 2), "fault": rig.fault}
            rig.buffer.setActive(True)
            rig.rendering = True
            rig.frame_no = fc
            self.pending.append(rig)

    def _process(self, rig: Rig, raw, shape, stamp: datetime, labels=None, meta=None) -> None:
        """Фоновый поток: перекладка в широкоугольный кадр, неисправности, подпись, JPEG."""
        try:
            cid = rig.spec.id
            arr = np.frombuffer(memoryview(raw), dtype=np.uint8).reshape(shape)
            if cid == "overview":
                img = cv2.flip(arr, 0)
            else:
                img = cv2.remap(arr, self.dist.map_x, self.dist.map_y, cv2.INTER_LINEAR)
            del raw, arr  # кадр в памяти текстуры больше не нужен
            rig.busy = False
            if img.shape[2] == 4:
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)  # после перекладки: пикселей меньше
            if cid != "overview":
                if rig.fault == "black":
                    img = np.zeros_like(img)
                elif rig.fault == "noise":
                    img = cv2.GaussianBlur(img, (0, 0), 6)
                    img = cv2.add(img, np.random.randint(0, 60, img.shape, dtype=np.uint8))
                elif rig.fault == "fog":  # густой туман: контраст почти пропал
                    img = cv2.addWeighted(cv2.GaussianBlur(img, (0, 0), 3), 0.12, np.full_like(img, 205), 0.88, 0)
                elif rig.fault == "covered":  # объектив закрыт (пакет, наклейка): тёмное пятно без деталей
                    img = self._covered(img.shape)
                img = self._sensor_noise(img)
                self._osd(img, cid, stamp)
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.cs.jpeg_quality])
            if ok:
                data = jpg.tobytes()
                if rig.fault is None:
                    self.frozen[cid] = data
                self.hub.publish(cid, data, labels, meta)
        except Exception as e:  # поток камеры не должен падать
            rig.busy = False
            print("camera", rig.spec.id, e)

    _cover_img: np.ndarray | None = None
    _noise: list | None = None

    def _sensor_noise(self, img: np.ndarray) -> np.ndarray:
        """Шум матрицы ±2 уровня: у настоящей камеры соседние кадры никогда не совпадают до пикселя,
        иначе пустая неподвижная сцена выглядит для центра как зависшее изображение."""
        if self._noise is None or self._noise[0][0].shape != img.shape:
            rng = np.random.default_rng(7)
            self._noise = [(rng.integers(0, 3, img.shape, dtype=np.uint8), rng.integers(0, 3, img.shape, dtype=np.uint8))
                           for _ in range(6)]
        pos, neg = self._noise[np.random.randint(len(self._noise))]
        return cv2.subtract(cv2.add(img, pos), neg)

    def _covered(self, shape) -> np.ndarray:
        if self._cover_img is None or self._cover_img.shape != shape:
            h, w = shape[:2]
            yy, xx = np.mgrid[0:h, 0:w]
            r = np.hypot((xx - w * 0.45) / w, (yy - h * 0.55) / h)
            v = np.clip(36 - r * 30, 20, 36)
            img = np.stack([v * 0.8, v * 0.9, v], axis=-1)
            self._cover_img = (img + np.random.normal(0, 1.2, img.shape)).clip(0, 255).astype(np.uint8)
        return self._cover_img.copy()

    @staticmethod
    def _osd(img: np.ndarray, cid: str, stamp: datetime) -> None:
        text = f"{stamp:%Y-%m-%d %H:%M:%S}  {cid.upper()}"
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        roi = img[6:14 + th, 6:18 + tw]
        roi[:] = (roi * 0.35).astype(np.uint8)
        cv2.putText(img, text, (12, 8 + th), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

    # ------------------------------------------------------------ сведения для API
    @staticmethod
    def ptz_state(rig: Rig) -> dict:
        lo, hi = rig.tilt_range()
        return {"pan_deg": round(rig.pan, 2), "tilt_deg": round(rig.tilt, 2),
                "goal": {"pan_deg": round(rig.pan_goal, 2), "tilt_deg": round(rig.tilt_goal, 2)},
                "moving": rig.moving,
                "azimuth_deg": round((rig.home_az + rig.pan) % 360, 2), "tilt_down_deg": round(rig.home_tilt + rig.tilt, 2),
                "home": {"azimuth_deg": round(rig.home_az, 2), "tilt_down_deg": round(rig.home_tilt, 2),
                         "target": {"x": round(float(rig.spec.home[0]), 2), "y": round(float(rig.spec.home[1]), 2)}},
                "limits": {"pan_deg": [-PAN_LIMIT_DEG, PAN_LIMIT_DEG], "tilt_deg": [round(lo, 2), round(hi, 2)]},
                "speed_dps": {"pan": PAN_SPEED_DPS, "tilt": TILT_SPEED_DPS}}

    def describe(self, proj) -> list[dict]:
        out = []
        for rig in self.rigs.values():
            sp = rig.spec
            lat, lon = proj.to_latlon(sp.pos[0], sp.pos[1])
            v = sp.target - sp.pos
            tilt = math.degrees(math.atan2(-v[2], math.hypot(v[0], v[1])))
            out.append({
                "id": sp.id, "object_id": sp.object_id, "name": sp.name,
                "stream_url": f"/cam/{sp.id}.mjpg", "snapshot_url": f"/cam/{sp.id}.jpg",
                "fault": rig.fault,
                "ptz": self.ptz_state(rig),
                "position": {"x": round(float(sp.pos[0]), 2), "y": round(float(sp.pos[1]), 2),
                             "lat": round(lat, 7), "lon": round(lon, 7), "height_m": float(sp.pos[2])},
                "view": {"azimuth_deg": round(heading_deg(v[:2]), 1), "tilt_down_deg": round(tilt, 1),
                         "target": {"x": round(float(sp.target[0]), 2), "y": round(float(sp.target[1]), 2)}},
                "image": {"width": self.cs.width, "height": self.cs.height, "fps": self.cs.fps},
                "calibration": {"model": "opencv", "K": self.dist.K.round(3).tolist(),
                                "D": self.dist.D.tolist(), "hfov_deg": round(self.dist.hfov_out, 1)},
            })
        return out
