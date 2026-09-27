"""Геометрия объекта: калибровка камер и зоны в метрах.

Координаты — локальные метры объекта: e — на восток, n — на север, начало в центре объекта.
Камера описана паспортом: положение и высота, точка, куда направлена ось (крен нулевой),
матрица K и радиальное искажение D широкоугольного объектива (модель OpenCV).
Детекция переводится в точку на земле: нижняя середина рамки → луч камеры → пересечение с z = 0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

CROSSWALK_W = 4.0     # ширина перехода вдоль дороги, м
WAIT_DEPTH = 4.0      # глубина зоны ожидания на тротуаре, м
WAIT_MARGIN = 1.5     # зона ожидания шире перехода на столько с каждой стороны, м
APPROACH_LEN = 90.0   # длина зоны подхода, м


class CameraModel:
    def __init__(self, cam: dict):
        self.id = cam["id"]
        p = cam["pose"]
        self.pos = np.array([p["e"], p["n"], p["h"]], dtype=np.float64)
        tgt = np.array([p["target_e"], p["target_n"], 0.0])
        f = tgt - self.pos
        self.f = f / np.linalg.norm(f)
        r = np.cross(self.f, [0.0, 0.0, 1.0])
        self.r = r / np.linalg.norm(r)
        self.u = np.cross(self.r, self.f)
        self.K = np.array(cam["K"], dtype=np.float64)
        self.D = np.array(cam["D"], dtype=np.float64)
        self.w = cam["image"]["width"]
        self.h = cam["image"]["height"]

    def undistort(self, uv: np.ndarray) -> np.ndarray:
        """Пиксели → нормированные координаты без искажения: обратное r_d = r_u(1 + k1 r_u² + k2 r_u⁴) методом Ньютона."""
        xd = (uv[:, 0] - self.K[0, 2]) / self.K[0, 0]
        yd = (uv[:, 1] - self.K[1, 2]) / self.K[1, 1]
        rd = np.hypot(xd, yd)
        ru = rd.copy()
        k1, k2 = self.D[0], self.D[1]
        for _ in range(30):
            r2 = ru * ru
            f = ru * (1 + k1 * r2 + k2 * r2 * r2) - rd
            ru = ru - f / (1 + 3 * k1 * r2 + 5 * k2 * r2 * r2)
        scale = np.where(rd > 1e-12, ru / np.maximum(rd, 1e-12), 1.0)
        return np.column_stack([xd * scale, yd * scale])

    def pixels_to_ground(self, uv: np.ndarray, max_dist: float = 150.0) -> np.ndarray:
        """Пиксели (N×2) → точки на земле (N×2); NaN, если луч не падает на землю перед камерой."""
        uv = np.asarray(uv, dtype=np.float64).reshape(-1, 1, 2)
        if len(uv) == 0:
            return np.zeros((0, 2))
        xy = self.undistort(uv.reshape(-1, 2))
        rays = self.f[None, :] + xy[:, :1] * self.r[None, :] - xy[:, 1:2] * self.u[None, :]
        out = np.full((len(xy), 2), np.nan)
        down = rays[:, 2] < -1e-6
        t = -self.pos[2] / np.where(down, rays[:, 2], -1.0)
        pts = self.pos[None, :2] + t[:, None] * rays[:, :2]
        ok = down & (np.hypot(*(pts - self.pos[None, :2]).T) < max_dist)
        out[ok] = pts[ok]
        return out

    def ground_to_pixels(self, en: np.ndarray, z: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """Точки на земле (N×2) → пиксели (N×2) и признак «перед камерой»."""
        en = np.asarray(en, dtype=np.float64).reshape(-1, 2)
        v = np.column_stack([en, np.full(len(en), z)]) - self.pos[None, :]
        xc, yc, zc = v @ self.r, -(v @ self.u), v @ self.f
        front = zc > 0.5
        zs = np.where(front, zc, 1.0)
        x, y = xc / zs, yc / zs
        r2 = x * x + y * y
        k1, k2 = self.D[0], self.D[1]
        d = 1 + k1 * r2 + k2 * r2 * r2
        u = self.K[0, 0] * x * d + self.K[0, 2]
        w = self.K[1, 1] * y * d + self.K[1, 2]
        # за пределами применимости модели искажения (очень широкий угол) точку считаем невидимой
        front &= (r2 < 4.0) & (d > 0.2)
        return np.column_stack([u, w]), front

    def sees(self, en: np.ndarray, margin: int = 10) -> np.ndarray:
        px, front = self.ground_to_pixels(en)
        return front & (px[:, 0] > margin) & (px[:, 0] < self.w - margin) & (px[:, 1] > margin) & (px[:, 1] < self.h - margin)


def cross2(a, b) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    n = float(np.hypot(*v))
    return v / n if n > 1e-9 else np.array([1.0, 0.0])


def _rect(center, along, across, half_along: float, half_across: float) -> np.ndarray:
    c, a, b = np.asarray(center), _unit(along), _unit(across)
    return np.array([c + a * half_along + b * half_across, c - a * half_along + b * half_across,
                     c - a * half_along - b * half_across, c + a * half_along - b * half_across])


def point_in_poly(p, poly: np.ndarray) -> bool:
    return cv2.pointPolygonTest(poly.astype(np.float32), (float(p[0]), float(p[1])), False) >= 0


@dataclass(eq=False)
class CrosswalkZone:
    id: str
    group: str
    a: np.ndarray
    b: np.ndarray
    band: np.ndarray                    # сам переход
    wait: tuple[np.ndarray, np.ndarray]  # зоны ожидания у концов a и b

    @property
    def center(self) -> np.ndarray:
        return (self.a + self.b) / 2


@dataclass(eq=False)
class Approach:
    """Подход к объекту: полосы, идущие к центру. Ось подхода направлена от центра наружу,
    встречные к центру полосы — слева от неё (правостороннее движение)."""
    street: str
    group: str
    pts: np.ndarray
    in_width: float
    stop_s: float                        # расстояние от центра до стоп-линии по оси подхода
    cum: np.ndarray = field(init=False)

    def __post_init__(self):
        seg = np.diff(self.pts, axis=0)
        self.cum = np.concatenate([[0.0], np.cumsum(np.hypot(seg[:, 0], seg[:, 1]))])

    def locate(self, p) -> tuple[float, float]:
        """Точка → (s — расстояние от центра вдоль подхода, d — смещение влево от оси)."""
        best = (1e9, 0.0, 0.0)
        for i in range(len(self.pts) - 1):
            a, b = self.pts[i], self.pts[i + 1]
            ab = b - a
            L = float(np.hypot(*ab))
            if L < 1e-6:
                continue
            t = max(0.0, min(1.0, float(np.dot(p - a, ab)) / (L * L)))
            q = a + ab * t
            dist = float(np.hypot(*(p - q)))
            if dist < best[0]:
                left = cross2(ab / L, p - a)  # >0 — слева от направления «от центра»
                best = (dist, self.cum[i] + t * L, left)
        return best[1], best[2]

    def contains(self, p) -> tuple[bool, float]:
        s, d = self.locate(p)
        return (-0.8 <= d <= self.in_width + 1.2 and self.stop_s - 3 <= s <= APPROACH_LEN), s

    def region(self) -> np.ndarray:
        """Контур зоны подхода для отрисовки."""
        left, right = [], []
        for i, p in enumerate(self.pts):
            if self.cum[i] > APPROACH_LEN:
                break
            k = min(i, len(self.pts) - 2)
            d = _unit(self.pts[k + 1] - self.pts[k])
            nrm = np.array([-d[1], d[0]])
            s = max(self.cum[i], self.stop_s)
            q = self.pts[0] + d * s if i == 0 else p
            left.append(q + nrm * self.in_width)
            right.append(q)
        return np.array(right + left[::-1]) if len(left) > 1 else np.zeros((0, 2))


class SiteGeometry:
    def __init__(self, site: dict):
        self.site = site
        self.image_zones: ImageZones | None = None
        self.crosswalks: list[CrosswalkZone] = []
        for cw in site.get("crosswalks", []):
            a, b = np.array(cw["a"], float), np.array(cw["b"], float)
            across = _unit(b - a)
            along = np.array([-across[1], across[0]])
            L = float(np.hypot(*(b - a)))
            band = _rect((a + b) / 2, across, along, L / 2, CROSSWALK_W / 2)
            wa = _rect(a - across * WAIT_DEPTH / 2, across, along, WAIT_DEPTH / 2, CROSSWALK_W / 2 + WAIT_MARGIN)
            wb = _rect(b + across * WAIT_DEPTH / 2, across, along, WAIT_DEPTH / 2, CROSSWALK_W / 2 + WAIT_MARGIN)
            self.crosswalks.append(CrosswalkZone(cw["id"], cw["group"], a, b, band, (wa, wb)))
        self.approaches: list[Approach] = []
        for arm in site.get("arms", []):
            pts = np.array(arm["pts"], float)
            if len(pts) < 2:
                continue
            group = "veh" if site["kind"] == "crossing" else f"veh_{arm.get('axis', 'A')}"
            d = _unit(pts[min(1, len(pts) - 1)] - pts[0])
            # стоп-линия — перед переходом на этом подходе, если он есть
            stop = max(arm["in_width_m"], arm["out_width_m"]) + 1.5
            for cw in self.crosswalks:
                s = float(np.dot(cw.center - pts[0], d))
                lat = abs(cross2(d, cw.center - pts[0]))
                if s > 0 and lat < max(arm["in_width_m"], arm["out_width_m"]) + 2:
                    stop = max(stop, s + CROSSWALK_W / 2 + 1.0)
            self.approaches.append(Approach(arm["street"], group, pts, arm["in_width_m"], stop))

    def crosswalk_at(self, p) -> tuple[CrosswalkZone | None, str | None, int | None]:
        """Где пешеход: ('wait', сторона) у перехода, ('cross', None) на переходе, иначе (None, None).
        Если для зоны нарисован многоугольник на кадре камеры, решает он, иначе расчётная зона в метрах."""
        iz = self.image_zones
        for cw in self.crosswalks:
            inside = iz.contains(("crosswalk", cw.id), p) if iz else None
            if inside if inside is not None else point_in_poly(p, cw.band):
                return cw, "cross", None
            for side in (0, 1):
                inside = iz.contains(("wait", cw.id, side), p) if iz else None
                if inside if inside is not None else point_in_poly(p, cw.wait[side]):
                    return cw, "wait", side
        return None, None, None

    def approach_at(self, p) -> tuple[Approach | None, float]:
        iz = self.image_zones
        for i, ap in enumerate(self.approaches):
            inside = iz.contains(("approach", i), p) if iz else None
            if inside is None:
                ok, s = ap.contains(p)
            else:
                ok, s = inside, ap.locate(p)[0]
            if ok:
                return ap, s
        return None, 0.0

    # ---------- зоны на кадрах камер ----------
    def zone_targets(self, site: dict) -> list[dict]:
        """Все зоны, которые можно разметить на кадре: ключ, тип, подпись."""
        titles = {g["id"]: g["title"] for g in site.get("signal_groups", [])}
        out = []
        for k, cw in enumerate(self.crosswalks, 1):
            name = titles.get(cw.group, cw.group)
            if len(self.crosswalks) > 1:
                name += f", переход {k}"
            out.append({"key": ["crosswalk", cw.id], "type": "crosswalk", "label": f"Переход · {name}"})
            for side in (0, 1):
                out.append({"key": ["wait", cw.id, side], "type": "wait", "label": f"Ожидание · {name} · сторона {side + 1}"})
        for i, ap in enumerate(self.approaches):
            b = heading(ap.pts[min(1, len(ap.pts) - 1)] - ap.pts[0])
            frm = ["с севера", "с северо-востока", "с востока", "с юго-востока", "с юга", "с юго-запада", "с запада", "с северо-запада"][int((b + 22.5) % 360 // 45)]
            out.append({"key": ["approach", i], "type": "approach", "label": f"Подход · {ap.street} {frm}"})
        return out

    def ground_zone(self, key) -> np.ndarray:
        kind = key[0]
        if kind == "approach":
            return self.approaches[key[1]].region()
        cw = next(c for c in self.crosswalks if c.id == key[1])
        return cw.band if kind == "crosswalk" else cw.wait[key[2]]

    def auto_zones(self, cam: "CameraModel") -> list[dict]:
        """Стартовая разметка камеры: расчётные зоны, спроецированные в кадр (координаты 0…1)."""
        out = []
        for t in self.zone_targets(self.site):
            poly = self.ground_zone(t["key"])
            if len(poly) < 3:
                continue
            dense = np.concatenate([np.linspace(poly[i], poly[(i + 1) % len(poly)], 24, endpoint=False) for i in range(len(poly))])
            px, front = cam.ground_to_pixels(dense)
            px = px[front]
            if len(px) < 3:
                continue
            px[:, 0] = np.clip(px[:, 0], 0, cam.w - 1)
            px[:, 1] = np.clip(px[:, 1], 0, cam.h - 1)
            # контур зоны по порядку (не выпуклая оболочка: подход на изогнутой дороге не должен захватывать тротуар)
            approx = cv2.approxPolyDP(px.astype(np.float32).reshape(-1, 1, 2), 2.5, True).reshape(-1, 2)
            if len(approx) < 3 or cv2.contourArea(approx) < 150:
                continue
            out.append({"key": t["key"], "points": [[round(float(x) / cam.w, 4), round(float(y) / cam.h, 4)] for x, y in approx]})
        return out

    def set_image_zones(self, cams: list["CameraModel"], zones: dict[str, list[dict]] | None) -> None:
        self.image_zones = ImageZones(cams, zones) if zones else None

    def group_points(self) -> dict[str, list[np.ndarray]]:
        """Контрольные точки зон каждой группы — чтобы понять, какая камера что видит."""
        out: dict[str, list[np.ndarray]] = {}
        for cw in self.crosswalks:
            out.setdefault(cw.group, []).extend([cw.wait[0].mean(axis=0), cw.wait[1].mean(axis=0), cw.center])
        for ap in self.approaches:
            d = _unit(ap.pts[1] - ap.pts[0])
            nrm = np.array([-d[1], d[0]])
            out.setdefault(ap.group, []).extend(ap.pts[0] + d * s + nrm * ap.in_width / 2 for s in (ap.stop_s + 5, ap.stop_s + 25))
        return out

    def coverage(self, cams: list[CameraModel]) -> dict[str, set[str]]:
        """Камера → зоны каких групп она видит (хотя бы половину контрольных точек)."""
        pts = self.group_points()
        cover: dict[str, set[str]] = {c.id: set() for c in cams}
        for c in cams:
            for g, ps in pts.items():
                seen = c.sees(np.array(ps))
                if seen.mean() >= 0.5:
                    cover[c.id].add(g)
        return cover


def heading(v) -> float:
    return math.degrees(math.atan2(v[0], v[1])) % 360


class ImageZones:
    """Зоны, нарисованные на кадрах камер (координаты 0…1). Точка на земле проецируется в кадр
    каждой камеры, у которой есть эта зона, и проверяется по многоугольнику."""

    def __init__(self, cams: list[CameraModel], zones: dict[str, list[dict]]):
        self.by_key: dict[tuple, list[tuple[CameraModel, np.ndarray]]] = {}
        models = {c.id: c for c in cams}
        for cid, items in zones.items():
            cam = models.get(cid)
            if cam is None:
                continue
            for z in items:
                pts = np.array(z["points"], dtype=np.float32) * np.array([cam.w, cam.h], dtype=np.float32)
                if len(pts) >= 3:
                    self.by_key.setdefault(tuple(z["key"]), []).append((cam, pts))

    def contains(self, key: tuple, p) -> bool | None:
        items = self.by_key.get(tuple(key))
        if not items:
            return None
        for cam, poly in items:
            px, front = cam.ground_to_pixels(np.asarray(p, dtype=np.float64).reshape(1, 2))
            if front[0] and cv2.pointPolygonTest(poly, (float(px[0, 0]), float(px[0, 1])), False) >= 0:
                return True
        return False
