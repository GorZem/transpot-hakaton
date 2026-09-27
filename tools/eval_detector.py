"""Точность детектора по точной разметке эмулятора (GET /cam/{id}/labeled).

Считает долю найденных людей и машин (совпадение рамок по IoU) для нескольких моделей и размеров
кадра на одних и тех же кадрах. Нужна, чтобы выбрать конфигурацию под сервер без видеокарты.
Запуск: python tools/eval_detector.py [--frames 40] [--configs yolo11n.pt:640 yolo11s.pt:960]
"""
from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.request

import cv2
import numpy as np

VEH = {"car", "bus", "truck", "emergency"}
COCO_VEH = {2, 3, 5, 7}


def iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emulator", default="http://127.0.0.1:8100")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--min-px", type=int, default=16)
    ap.add_argument("--device", default="0")
    ap.add_argument("--configs", nargs="+", default=["yolo11n.pt:640", "yolo11n.pt:960", "yolo11s.pt:640", "yolo11s.pt:960"])
    args = ap.parse_args()
    cams = [c["id"] for c in json.load(urllib.request.urlopen(args.emulator + "/api/cameras", timeout=20))]
    samples = []
    for k in range(args.frames):
        cid = cams[k % len(cams)]
        d = json.load(urllib.request.urlopen(f"{args.emulator}/cam/{cid}/labeled?min_px={args.min_px}", timeout=30))
        img = cv2.imdecode(np.frombuffer(base64.b64decode(d["image_jpeg_base64"]), np.uint8), 1)
        gt = [(("veh" if o["class"] in VEH else "person"), o["bbox"]) for o in d["objects"]
              if o["visible"] and o.get("occluded_frac", 0) < 0.5]
        samples.append((img, gt))
    n_person = sum(1 for _, g in samples for c, _ in g if c == "person")
    n_veh = sum(1 for _, g in samples for c, _ in g if c == "veh")
    print(f"Кадров {len(samples)}, людей {n_person}, машин {n_veh} (видны хотя бы наполовину, рамка от {args.min_px} px)")
    from ultralytics import YOLO
    for cfg in args.configs:
        name, sz = cfg.split(":")
        m = YOLO(name)
        dev = "cpu" if args.device == "cpu" else int(args.device)
        m.predict(samples[0][0], imgsz=int(sz), device=dev, verbose=False)
        found = {"person": 0, "veh": 0}
        t = time.perf_counter()
        for img, gt in samples:
            r = m.predict(img, imgsz=int(sz), conf=0.2, device=dev, verbose=False, classes=[0, 1, 2, 3, 5, 7])[0]
            dets = [("veh" if int(c) in COCO_VEH else "person", b) for b, c in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.cls.cpu().numpy())]
            used = set()
            for cls, box in gt:
                best, bi = 0.0, None
                for i, (dc, db) in enumerate(dets):
                    if dc == cls and i not in used:
                        v = iou(box, db)
                        if v > best:
                            best, bi = v, i
                if best >= 0.4:
                    used.add(bi)
                    found[cls] += 1
        ms = (time.perf_counter() - t) * 1000 / len(samples)
        print(f"{cfg:16} люди {found['person'] / max(1, n_person):5.0%}  машины {found['veh'] / max(1, n_veh):5.0%}  ({ms:.0f} мс/кадр на {args.device})")


if __name__ == "__main__":
    main()
