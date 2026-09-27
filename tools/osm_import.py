"""Импорт объектов пилота (Люблино) из OpenStreetMap.

Для каждого объекта берёт из OSM:
- улицы, на которых он стоит;
- направление главной дороги (азимут), чтобы ориентировать камеры;
- адреса ближайших домов для поиска в админке.

Результат: data/sites.json. Запуск: python tools/osm_import.py
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
UA = {"User-Agent": "smart-crossing-hackathon/1.0 (educational)"}
BBOX = (55.6680, 37.7410, 55.6850, 37.7650)  # юг, запад, север, восток

# Точки из OSM (узлы светофоров), проверены на карте участка.
SEEDS = [  # id, тип, название, главная улица, широта, долгота
    ("p-krasnodonskaya-south", "crossing", "Переход на Краснодонской у ул. Судакова", "Краснодонская улица", 55.67288, 37.74887),
    ("p-krasnodonskaya-mid", "crossing", "Переход на Краснодонской, середина квартала", "Краснодонская улица", 55.67436, 37.74897),
    ("p-krasnodonskaya-north", "crossing", "Переход на Краснодонской у Краснодарской", "Краснодонская улица", 55.67541, 37.74904),
    ("p-krasnodarskaya", "crossing", "Переход на Краснодарской у Таганрогской", "Краснодарская улица", 55.67782, 37.75380),
    ("p-stavropolskaya", "crossing", "Переход на Ставропольской у Таганрогской", "Ставропольская улица", 55.68125, 37.75514),
    ("x-krasnodarskaya-krasnodonskaya", "cross", "Краснодарская × Краснодонская", "Краснодонская улица", 55.67751, 37.74918),
    ("x-krasnodonskaya-stavropolskaya", "cross", "Краснодонская × Ставропольская", "Краснодонская улица", 55.68089, 37.74939),
    ("x-krasnodarskaya-novorossiyskaya", "cross", "Краснодарская × Новороссийская", "Новороссийская улица", 55.67719, 37.75931),
    ("x-krasnodonskaya-sovkhoznaya", "cross", "Краснодонская × Совхозная", "Краснодонская улица", 55.67113, 37.74873),
    ("t-novorossiyskaya-stavropolskaya", "tee", "Новороссийская × Ставропольская", "Ставропольская улица", 55.68164, 37.76105),
]
ROADS = "primary|secondary|tertiary|residential|unclassified"


def overpass(query: str) -> list[dict]:
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in OVERPASS:
        try:
            req = urllib.request.Request(url, data=body, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=120))["elements"]
        except Exception as e:  # сервер занят — пробуем зеркало
            last = e
    raise RuntimeError(f"Overpass недоступен: {last}")


def dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    dy = (b[0] - a[0]) * 111_320
    dx = (b[1] - a[1]) * 111_320 * math.cos(math.radians(a[0]))
    return (math.degrees(math.atan2(dx, dy)) + 360) % 180  # направление оси дороги, 0..180


def seg_dist(p, a, b) -> tuple[float, float]:
    """Расстояние от точки до отрезка (м) и азимут отрезка."""
    k = math.cos(math.radians(p[0])) * 111_320
    px, py = p[1] * k, p[0] * 111_320
    ax, ay, bx, by = a[1] * k, a[0] * 111_320, b[1] * k, b[0] * 111_320
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy), bearing(a, b)


def main() -> None:
    s, w, n, e = BBOX
    elements = overpass(
        f'[out:json][timeout:90];('
        f'way["highway"~"^({ROADS})$"]({s},{w},{n},{e});'
        f'way["building"]["addr:housenumber"]({s},{w},{n},{e});'
        f'node["addr:housenumber"]({s},{w},{n},{e});'
        f');out geom;'
    )
    roads, houses = [], []
    for el in elements:
        tags = el.get("tags", {})
        if "highway" in tags and "geometry" in el:
            pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
            roads.append((tags.get("name"), tags["highway"], pts))
        elif "addr:housenumber" in tags and tags.get("addr:street"):
            if "geometry" in el:
                pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
                c = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            else:
                c = (el["lat"], el["lon"])
            houses.append((f'{tags["addr:street"]}, {tags["addr:housenumber"]}', c))

    sites = []
    for sid, kind, title, main_street, lat, lon in SEEDS:
        p = (lat, lon)
        near: dict[str, tuple[float, float]] = {}  # улица -> (расстояние, азимут)
        for name, _hw, pts in roads:
            if not name:
                continue
            for a, b in zip(pts, pts[1:]):
                d, br = seg_dist(p, a, b)
                if d < 30 and (name not in near or d < near[name][0]):
                    near[name] = (d, br)
        streets = sorted(near, key=lambda k: near[k][0])
        streets = [main_street] + [x for x in streets if x != main_street]
        main_bearing = round(near[main_street][1], 1) if main_street in near else 0.0
        addrs = sorted({h for h in houses if dist_m(p, h[1]) < 250}, key=lambda h: dist_m(p, h[1]))
        seen, addresses = set(), []
        for a, c in addrs:
            if a not in seen:
                seen.add(a)
                addresses.append({"address": a, "distance_m": round(dist_m(p, c))})
            if len(addresses) == 6:
                break
        sites.append({
            "id": sid, "kind": kind, "title": title, "lat": lat, "lon": lon,
            "main_street": main_street, "streets": streets, "road_bearing_deg": main_bearing, "addresses": addresses,
        })
        print(f"{sid:36} {', '.join(streets):55} {len(addresses)} адр.")

    out = Path(__file__).resolve().parent.parent / "data" / "sites.json"
    out.write_text(json.dumps({"district": "Люблино, Москва", "source": "OpenStreetMap (ODbL)", "sites": sites},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
