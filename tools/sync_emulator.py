"""Паспорта объектов из эмулятора участка (аналог исполнительной съёмки при монтаже).

На реальном объекте при монтаже фиксируют: где стоят светофоры и какие у контроллера группы
сигналов, геометрию переходов и подходов, положение и калибровку камер. Здесь «улицей» служит
эмулятор, поэтому эти данные забираются из его API. Адреса ближайших домов — из OpenStreetMap.

Результат: data/sites.json. Запуск (эмулятор должен работать):
    python tools/sync_emulator.py [--url http://127.0.0.1:8100]
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERPASS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
UA = {"User-Agent": "smart-crossing-hackathon/1.0 (educational)"}

# Светофорные перекрёстки без камер (в эмуляторе работают по местной программе): постоянные ID и названия.
UNEQUIPPED_NAMES = {
    "x-krasnodarskaya-sovkhoznaya": "Краснодарская × Совхозная",
    "t-sovkhoznaya-novorossiyskaya-w": "Совхозная × Новороссийская, западное примыкание",
    "t-sovkhoznaya-novorossiyskaya-c": "Совхозная × Новороссийская, среднее примыкание",
    "t-sovkhoznaya-novorossiyskaya-e": "Совхозная × Новороссийская, восточное примыкание",
}


def get(url: str):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))


def overpass(query: str) -> list[dict]:
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in OVERPASS:
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, data=body, headers=UA), timeout=120))["elements"]
        except Exception as e:  # сервер занят — пробуем зеркало
            last = e
    raise RuntimeError(f"Overpass недоступен: {last}")


def bearing(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dx, dy)) % 360


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8100")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    objects = {o["id"]: o for o in get(base + "/api/objects")}
    emap = get(base + "/api/map")

    # Проекция эмулятора (локальные метры: x — восток, y — север) по любой камере с известными координатами.
    cam0 = next(c for o in objects.values() for c in o["cameras"])
    lat0 = cam0["position"]["lat"] - cam0["position"]["y"] / 111_320
    kx = 111_320 * math.cos(math.radians(lat0))
    lon0 = cam0["position"]["lon"] - cam0["position"]["x"] / kx
    to_ll = lambda x, y: (round(lat0 + y / 111_320, 7), round(lon0 + x / kx, 7))

    sites = []
    def degree(sig) -> int:
        return sum(1 for r in emap["roads"] for p in (r["pts"][0], r["pts"][-1])
                   if math.hypot(p[0] - sig["x"], p[1] - sig["y"]) < 3)
    # Имена неоснащённым: крестовой — «Краснодарская × Совхозная», Т-образные — с запада на восток.
    unequipped = [s for s in emap["signals"] if not s["object_id"]]
    crosses = [s for s in unequipped if degree(s) >= 4]
    tees = sorted((s for s in unequipped if degree(s) < 4), key=lambda s: s["x"])
    name_of: dict[int, str] = {}
    if unequipped:  # сейчас все объекты эмулятора оснащены; ветка нужна, если появятся новые без камер
        assert len(crosses) == 1 and len(tees) == 3, "в эмуляторе изменился состав перекрёстков: обновите UNEQUIPPED_NAMES"
        name_of = {crosses[0]["node_id"]: "x-krasnodarskaya-sovkhoznaya"}
        name_of.update({t["node_id"]: sid for t, sid in zip(tees, ["t-sovkhoznaya-novorossiyskaya-w",
                                                                  "t-sovkhoznaya-novorossiyskaya-c",
                                                                  "t-sovkhoznaya-novorossiyskaya-e"])})
    for sig in sorted(emap["signals"], key=lambda s: s["object_id"] or "~"):
        cx, cy = sig["x"], sig["y"]
        loc = lambda p: [round(p[0] - cx, 2), round(p[1] - cy, 2)]
        arms = []
        for r in emap["roads"]:
            pts = r["pts"]
            if math.hypot(pts[0][0] - cx, pts[0][1] - cy) < 3:
                away, in_left = pts, True       # полилиния от узла: встречные к узлу полосы слева
                w_in, w_out = r["left"], r["right"]
            elif math.hypot(pts[-1][0] - cx, pts[-1][1] - cy) < 3:
                away, in_left = pts[::-1], False
                w_in, w_out = r["right"], r["left"]
            else:
                continue
            k = min(len(away) - 1, 1)
            while k < len(away) - 1 and math.hypot(away[k][0] - cx, away[k][1] - cy) < 15:
                k += 1
            arms.append({"street": r["name"], "bearing_deg": round(bearing(away[k][0] - cx, away[k][1] - cy), 1),
                         "in_width_m": round(w_in, 2), "out_width_m": round(w_out, 2),
                         "pts": [loc(p) for p in away if math.hypot(p[0] - cx, p[1] - cy) < 150]})
        crosswalks = [{"id": f"cw{c['id']}", "group": c["group"], "a": loc(c["a"]), "b": loc(c["b"])}
                      for c in emap["crosswalks"] if c["node_id"] == sig["node_id"]]
        lat, lon = to_ll(cx, cy)
        oid = sig["object_id"]
        if oid:
            o = objects[oid]
            truth = get(f"{base}/api/objects/{oid}/truth")
            for arm in arms:  # ось подхода — как у контроллера объекта
                best = min(truth["approaches"], key=lambda a: abs((a["bearing_deg"] - arm["bearing_deg"] + 180) % 360 - 180))
                arm["axis"] = best["axis"]
            site = {
                "id": oid, "kind": o["kind"], "title": o["title"], "lat": lat, "lon": lon,
                "equipped": True, "streets": o["streets"], "axes": o["axes"],
                "signal_groups": [{"id": g, "kind": v["kind"], "title": v["label"]} for g, v in o["signal"]["groups"].items()],
                "crosswalks": crosswalks, "arms": arms,
                "cameras": [{
                    "id": c["id"], "title": c["name"], "stream": c["stream_url"],
                    "pose": {"e": round(c["position"]["x"] - cx, 2), "n": round(c["position"]["y"] - cy, 2),
                             "h": c["position"]["height_m"],
                             "target_e": round(c["view"]["target"]["x"] - cx, 2), "target_n": round(c["view"]["target"]["y"] - cy, 2)},
                    "image": {"width": c["image"]["width"], "height": c["image"]["height"]},
                    "K": c["calibration"]["K"], "D": c["calibration"]["D"], "hfov_deg": c["calibration"]["hfov_deg"],
                } for c in o["cameras"]],
                "controller": {"path": f"/api/objects/{oid}"},
            }
        else:
            sid = name_of[sig["node_id"]]
            kind = "cross" if len(arms) >= 4 else "tee"
            site = {"id": sid, "kind": kind, "title": UNEQUIPPED_NAMES[sid], "lat": lat, "lon": lon, "equipped": False,
                    "streets": sorted({a["street"] for a in arms}), "axes": {}, "signal_groups": [],
                    "crosswalks": crosswalks, "arms": arms, "cameras": [], "controller": None}
        sites.append(site)

    # Адреса ближайших домов для поиска.
    lats = [s["lat"] for s in sites]
    lons = [s["lon"] for s in sites]
    s_, w_, n_, e_ = min(lats) - 0.003, min(lons) - 0.005, max(lats) + 0.003, max(lons) + 0.005
    houses = []
    for el in overpass(f'[out:json][timeout:90];(way["building"]["addr:housenumber"]({s_},{w_},{n_},{e_});'
                       f'node["addr:housenumber"]({s_},{w_},{n_},{e_}););out center;'):
        t = el.get("tags", {})
        if not t.get("addr:street"):
            continue
        c = el.get("center") or {"lat": el["lat"], "lon": el["lon"]}
        houses.append((f'{t["addr:street"]}, {t["addr:housenumber"]}', c["lat"], c["lon"]))
    for s in sites:
        k = math.cos(math.radians(s["lat"])) * 111_320
        near = sorted(((math.hypot((lo - s["lon"]) * k, (la - s["lat"]) * 111_320), a) for a, la, lo in houses))
        seen, addrs = set(), []
        for d, a in near:
            if d > 250 or len(addrs) == 6:
                break
            if a not in seen:
                seen.add(a)
                addrs.append({"address": a, "distance_m": round(d)})
        s["addresses"] = addrs

    out = ROOT / "data" / "sites.json"
    out.write_text(json.dumps({"district": "Люблино, Москва", "source": f"эмулятор участка ({base}), адреса — OpenStreetMap",
                               "sites": sites}, ensure_ascii=False, indent=1), encoding="utf-8")
    for s in sites:
        print(f"{s['id']:36} {s['kind']:8} {'камер ' + str(len(s['cameras'])) if s['equipped'] else 'без камер':9} "
              f"переходов {len(s['crosswalks'])} подходов {len(s['arms'])}  {s['addresses'][0]['address'] if s['addresses'] else ''}")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
