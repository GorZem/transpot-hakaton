"""Offline run of the vision pipeline on a recorded video: detection + tracking + zones.

Prints per-video statistics and optionally writes an annotated mp4.
Usage:
  python scripts/replay.py --config config/default.yaml --camera cam1 [--out out.mp4] [--fps 6]
"""
import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402

from smartcross.config import load_config  # noqa: E402
from smartcross.vision.camera import draw_overlay  # noqa: E402
from smartcross.vision.detector import YoloDetector  # noqa: E402
from smartcross.vision.zones import ZoneAnalyzer  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--camera", required=True)
    ap.add_argument("--fps", type=float, default=6)
    ap.add_argument("--out")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cam = next(c for c in cfg.cameras if c.id == args.camera)
    det = YoloDetector(cfg.detector)
    za = ZoneAnalyzer(cam.zones)
    cap = cv2.VideoCapture(cam.source)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25
    step = max(1, round(src_fps / args.fps))
    writer = None
    i = 0
    frames = 0
    t_inf = 0.0
    passed = Counter()
    classes = Counter()
    peds = 0
    max_wait = 0
    wait_sum = 0
    emergency = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        i += 1
        if (i - 1) % step:
            continue
        t0 = time.perf_counter()
        dets = det(frame)
        t_inf += time.perf_counter() - t0
        h, w = frame.shape[:2]
        rep = za.analyze(dets, w, h)
        frames += 1
        passed.update(rep.veh_passed)
        classes.update(rep.veh_passed_by_class)
        peds += rep.ped_new
        waiting = sum(rep.ped_waiting.values())
        max_wait = max(max_wait, waiting)
        wait_sum += waiting
        emergency += rep.emergency
        if args.out:
            img = draw_overlay(frame, cam.zones, dets,
                               f"{cam.id} | ждут: {waiting} | на переходе: {rep.ped_on_crossing} | "
                               f"ТС в зоне: {sum(rep.veh_in_approach.values())} | проехало: {sum(passed.values())}")
            if writer is None:
                writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (w, h))
            writer.write(img)
    if writer:
        writer.release()
    dur = i / src_fps
    print(f"{cam.id}: {cam.source}")
    print(f"  длительность {dur:.1f} c, обработано кадров {frames}, инференс {1000 * t_inf / max(frames, 1):.0f} мс/кадр")
    print(f"  ТС проехало линию: {dict(passed)} (по классам {dict(classes)}), "
          f"≈ {sum(passed.values()) * 3600 / max(dur, 1):.0f} авт/ч")
    print(f"  уникальных пешеходов на переходе: {peds}")
    print(f"  ждут в зонах ожидания: в среднем {wait_sum / max(frames, 1):.1f}, максимум {max_wait}")
    print(f"  кадров со спецтранспортом: {emergency}")


if __name__ == "__main__":
    main()
