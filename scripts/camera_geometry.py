"""Camera placement calculator: ground coverage and pixel density for a pole-mounted camera.

Usage: python scripts/camera_geometry.py [--height 6] [--hfov 70] [--tilt 18] [--width 1920] [--imgsz 960]
"""
import argparse
import math


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--height", type=float, default=6.0, help="высота установки, м")
    ap.add_argument("--hfov", type=float, default=70.0, help="горизонтальный угол обзора, град")
    ap.add_argument("--tilt", type=float, default=18.0, help="наклон оптической оси вниз от горизонта, град")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height-px", type=int, default=1080)
    ap.add_argument("--imgsz", type=int, default=960, help="размер входа нейросети по длинной стороне")
    ap.add_argument("--person", type=float, default=1.7, help="рост человека, м")
    args = ap.parse_args()

    aspect = args.height_px / args.width
    vfov = 2 * math.degrees(math.atan(math.tan(math.radians(args.hfov / 2)) * aspect))
    near = args.height / math.tan(math.radians(args.tilt + vfov / 2))
    far_angle = args.tilt - vfov / 2
    far = args.height / math.tan(math.radians(far_angle)) if far_angle > 0 else math.inf
    f_px = (args.width / 2) / math.tan(math.radians(args.hfov / 2))  # focal length in pixels
    scale = args.imgsz / args.width

    print(f"Высота {args.height} м, HFOV {args.hfov}°, VFOV {vfov:.1f}°, наклон {args.tilt}°")
    print(f"Мёртвая зона под камерой: {near:.1f} м;  дальняя граница кадра: "
          f"{'горизонт' if far == math.inf else f'{far:.0f} м'}")
    print(f"\n| Дальность, м | Ширина обзора, м | Рост человека, px (кадр) | px на входе YOLO ({args.imgsz}) | Легковой (4.5 м), px |")
    print("|---|---|---|---|---|")
    for d in (10, 20, 30, 40, 50, 60, 80):
        slant = math.hypot(d, args.height)
        width_m = 2 * slant * math.tan(math.radians(args.hfov / 2))
        person_px = f_px * args.person / slant
        car_px = f_px * 4.5 / slant
        print(f"| {d} | {width_m:.0f} | {person_px:.0f} | {person_px * scale:.0f} | {car_px:.0f} |")


if __name__ == "__main__":
    main()
