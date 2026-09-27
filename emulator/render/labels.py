"""Разметка кадра камеры: рамки машин и людей в пикселях выходного (искажённого) кадра.

Считается в тот момент, когда кадр ставится на рендер, из того же состояния сцены,
поэтому рамки совпадают с изображением без рассинхрона.

- Рамка: 8 углов 3D-габарита агента → система координат камеры → модель объектива OpenCV (k1, k2) → min/max.
- Закрытость зданиями: лучи от камеры к 5 точкам агента, пересечение с гранями домов с учётом их высоты.
- Закрытость другими агентами: доля рамки, накрытая рамками более близких агентов (приближённо).
"""
from __future__ import annotations

import math

import numpy as np

from emulator.world.sim import World

PED_STATE = {"approach": "approaching", "wait": "waiting", "cross": "crossing", "leave": "leaving"}


class Labeler:
    def __init__(self, world: World, dist, cam_settings):
        self.world = world
        self.f = cam_settings.focal_px
        self.W, self.H = cam_settings.width, cam_settings.height
        self.k1, self.k2 = cam_settings.k1, cam_settings.k2
        edges, heights = [], []
        for b in world.net.buildings:
            pts = b["pts"]
            for i in range(len(pts)):
                a, c = pts[i], pts[(i + 1) % len(pts)]
                edges.append((a[0], a[1], c[0], c[1]))
                heights.append(b["height"])
        self.edges = np.array(edges, dtype=np.float64)
        self.heights = np.array(heights, dtype=np.float64)

    # ------------------------------------------------------------------ агенты → боксы
    def _boxes(self):
        ids, kinds, extra, corners, samples, centers = [], [], [], [], [], []
        for c in self.world.cars:
            h = c.height + (0.2 if c.kind == "emergency" else 0.0)
            ids.append(f"car-{c.id}")
            kinds.append(c.kind)
            extra.append({"speed_mps": round(c.v, 1)})
            corners.append(self._corners(c.x, c.y, c.heading, c.length, c.width, 0.0, h))
            samples.append(self._samples(c.x, c.y, c.heading, c.length, c.width, 0.0, h))
            centers.append((c.x, c.y, h / 2))
        for p in self.world.peds:
            s = p.height / 1.75
            z0 = 0.02 if p.state == "cross" else 0.15
            depth = (0.5 if p.moving else 0.32) * s
            ids.append(f"ped-{p.id}")
            kinds.append("person")
            extra.append({"state": PED_STATE[p.state], "crossing_on_red": bool(p.violation),
                          "crosswalk_id": p.cw.id, "group": p.group is not None})
            x, y = float(p.pos[0]), float(p.pos[1])
            corners.append(self._corners(x, y, p.heading, depth, 0.66 * s, z0, z0 + 1.74 * s))
            samples.append(self._samples(x, y, p.heading, depth, 0.66 * s, z0, z0 + 1.74 * s))
            centers.append((x, y, z0 + 0.9 * s))
        if not ids:
            return ids, kinds, extra, np.zeros((0, 8, 3)), np.zeros((0, 5, 3)), np.zeros((0, 3))
        return ids, kinds, extra, np.array(corners), np.array(samples), np.array(centers)

    @staticmethod
    def _corners(x, y, heading, length, width, z0, z1):
        ca, sa = math.cos(heading), math.sin(heading)
        out = []
        for dl in (-length / 2, length / 2):
            for dw in (-width / 2, width / 2):
                px, py = x + ca * dl - sa * dw, y + sa * dl + ca * dw
                out.append((px, py, z0))
                out.append((px, py, z1))
        return out

    @staticmethod
    def _samples(x, y, heading, length, width, z0, z1):
        ca, sa = math.cos(heading), math.sin(heading)
        zm, zt = z0 + (z1 - z0) * 0.5, z0 + (z1 - z0) * 0.9
        pts = [(x, y, zm)]
        for dl, dw in ((-0.35, -0.35), (0.35, 0.35), (-0.35, 0.35), (0.35, -0.35)):
            pts.append((x + ca * dl * length - sa * dw * width, y + sa * dl * length + ca * dw * width, zt))
        return pts

    # ------------------------------------------------------------------ проекция
    def _project(self, pts_cam: np.ndarray):
        """pts_cam (..., 3) в координатах камеры Panda3D (x вправо, y вперёд, z вверх) → пиксели."""
        depth = np.maximum(pts_cam[..., 1], 0.3)
        xn, yn = pts_cam[..., 0] / depth, -pts_cam[..., 2] / depth
        r2 = xn * xn + yn * yn
        k = 1 + self.k1 * r2 + self.k2 * r2 * r2
        return self.f * xn * k + self.W / 2, self.f * yn * k + self.H / 2

    def _building_occlusion(self, cam: np.ndarray, samples: np.ndarray) -> np.ndarray:
        """Доля точек агента, закрытых домами, для каждого агента."""
        n = samples.shape[0]
        if n == 0 or len(self.edges) == 0:
            return np.zeros(n)
        S = samples.reshape(-1, 3)
        near = np.hypot((self.edges[:, 0] + self.edges[:, 2]) / 2 - cam[0],
                        (self.edges[:, 1] + self.edges[:, 3]) / 2 - cam[1]) < 350
        E, Hb = self.edges[near], self.heights[near]
        px, py = cam[0], cam[1]
        rx, ry = S[:, 0:1] - px, S[:, 1:2] - py                 # (R,1)
        ax, ay = E[:, 0][None, :], E[:, 1][None, :]              # (1,E)
        sx, sy = (E[:, 2] - E[:, 0])[None, :], (E[:, 3] - E[:, 1])[None, :]
        den = rx * sy - ry * sx
        with np.errstate(divide="ignore", invalid="ignore"):
            t = ((ax - px) * sy - (ay - py) * sx) / den        # вдоль луча
            u = ((ax - px) * ry - (ay - py) * rx) / den        # вдоль грани
        hit = (np.abs(den) > 1e-9) & (t > 1e-3) & (t < 0.999) & (u >= 0) & (u <= 1)
        z_ray = cam[2] + (S[:, 2:3] - cam[2]) * t
        blocked = (hit & (Hb[None, :] > z_ray)).any(axis=1)
        return blocked.reshape(n, -1).mean(axis=1)

    def label(self, cam_mat: np.ndarray, cam_pos: np.ndarray) -> list[dict]:
        """cam_mat — матрица камеры в мир (4×4, векторы-строки, как в Panda3D)."""
        ids, kinds, extra, corners, samples, centers = self._boxes()
        if not ids:
            return []
        inv = np.linalg.inv(cam_mat)
        n = len(ids)
        homo = np.concatenate([corners, np.ones((n, 8, 1))], axis=2)
        pc = homo @ inv                                            # (n, 8, 4)
        depth = pc[..., 1]
        front = depth > 0.3
        keep = front.any(axis=1)
        u, v = self._project(pc[..., :3])
        W, H = self.W, self.H
        x1, x2 = u.min(axis=1), u.max(axis=1)
        y1, y2 = v.min(axis=1), v.max(axis=1)
        partly_behind = ~front.all(axis=1)
        cx1, cx2 = np.clip(x1, 0, W), np.clip(x2, 0, W)
        cy1, cy2 = np.clip(y1, 0, H), np.clip(y2, 0, H)
        keep &= (cx2 - cx1 > 1) & (cy2 - cy1 > 1)
        truncated = (x1 < 0) | (x2 > W) | (y1 < 0) | (y2 > H) | partly_behind
        idx = np.nonzero(keep)[0]
        if len(idx) == 0:
            return []
        b_occ = self._building_occlusion(cam_pos, samples[idx])
        dist = np.hypot(centers[idx, 0] - cam_pos[0], centers[idx, 1] - cam_pos[1])
        boxes = np.stack([cx1[idx], cy1[idx], cx2[idx], cy2[idx]], axis=1)
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        order = np.argsort(dist)
        a_occ = np.zeros(len(idx))
        for k, i in enumerate(order):
            if k == 0:
                continue
            nearer = order[:k]
            ix = np.clip(np.minimum(boxes[nearer, 2], boxes[i, 2]) - np.maximum(boxes[nearer, 0], boxes[i, 0]), 0, None)
            iy = np.clip(np.minimum(boxes[nearer, 3], boxes[i, 3]) - np.maximum(boxes[nearer, 1], boxes[i, 1]), 0, None)
            # ближние к камере агенты не закрываются стоящими за ними, но могут закрываться домами
            a_occ[i] = min(1.0, float((ix * iy).sum() / max(area[i], 1e-6)))
        out = []
        for j, i in enumerate(idx):
            occ = max(float(b_occ[j]), float(a_occ[j]))
            if b_occ[j] >= 0.999:
                continue  # полностью за домом
            out.append({
                "id": ids[i], "class": kinds[i],
                "bbox": [round(float(v_), 1) for v_ in boxes[j]],
                "occluded_frac": round(occ, 2),
                "occluded_by_buildings": round(float(b_occ[j]), 2),
                "truncated": bool(truncated[i]),
                "visible": occ < 0.9,
                "distance_m": round(float(dist[j]), 1),
                **extra[i],
            })
        return out
