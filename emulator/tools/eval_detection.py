"""Насколько детектор видит людей и машины эмулятора: полнота по разметке /cam/{id}/labeled.

Запуск (нужен python с ultralytics, например из окружения системы):
    python emulator/tools/eval_detection.py --model yolo11s.pt --rounds 3

Учитываются агенты, которые видны хотя бы наполовину и с рамкой не меньше --min-h пикселей по высоте.
Совпадение — IoU >= 0.4 с рамкой детектора подходящего класса.
"""
from __future__ import annotations

import argparse
import base64
import json
import urllib.request
from collections import defaultdict

import cv2
import numpy as np

COCO_VEH = {2: "car", 5: "bus", 7: "truck", 3: "motorcycle"}


def get(url: str):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8100")
    ap.add_argument("--model", default="yolo11s.pt")
    ap.add_argument("--imgsz", type=int, default=960)
    ap.add_argument("--conf", type=float, default=0.2)
    ap.add_argument("--rounds", type=int, default=2, help="сколько раз обойти все камеры")
    ap.add_argument("--min-h", type=float, default=24, help="минимальная высота рамки, пикселей")
    ap.add_argument("--save", default="", help="папка для кадров с отрисованными ошибками")
    args = ap.parse_args()

    from ultralytics import YOLO
    model = YOLO(args.model)
    cams = [c["id"] for c in get(args.url + "/api/cameras")]
    stats = defaultdict(lambda: [0, 0])  # класс -> [найдено, всего]
    conf_hits = defaultdict(list)
    for rnd in range(args.rounds):
        for cid in cams:
            d = get(f"{args.url}/cam/{cid}/labeled?min_px=1")
            if d.get("fault"):
                continue
            img = cv2.imdecode(np.frombuffer(base64.b64decode(d["image_jpeg_base64"]), np.uint8), 1)
            res = model.predict(img, imgsz=args.imgsz, conf=args.conf, verbose=False)[0]
            dets = [(int(c), float(s), b.tolist()) for c, s, b in
                    zip(res.boxes.cls.cpu().numpy(), res.boxes.conf.cpu().numpy(), res.boxes.xyxy.cpu().numpy())]
            for o in d["objects"]:
                x1, y1, x2, y2 = o["bbox"]
                if o["occluded_frac"] > 0.5 or o["truncated"] or y2 - y1 < args.min_h:
                    continue
                gt = "person" if o["class"] == "person" else "vehicle"
                ok_cls = {0} if gt == "person" else set(COCO_VEH)
                best = max(((iou(o["bbox"], b), s) for c, s, b in dets if c in ok_cls), default=(0.0, 0.0))
                hit = best[0] >= 0.4
                stats[gt][1] += 1
                stats[gt][0] += hit
                if hit:
                    conf_hits[gt].append(best[1])
                if args.save and not hit:
                    cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 2)
            if args.save:
                for c, s, b in dets:
                    cv2.rectangle(img, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (0, 255, 0), 1)
                cv2.imwrite(f"{args.save}/{cid}_{rnd}.jpg", img)
        print(f"круг {rnd + 1}/{args.rounds} готов")
    for k in ("person", "vehicle"):
        found, total = stats[k]
        mc = float(np.mean(conf_hits[k])) if conf_hits[k] else 0.0
        print(f"{k:8} найдено {found}/{total} = {100 * found / max(total, 1):.0f}%   средняя уверенность {mc:.2f}")


if __name__ == "__main__":
    main()
