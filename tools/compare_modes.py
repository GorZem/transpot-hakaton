"""Сравнение режимов на эмуляторе: «умный» (управляет центр) против штатной программы контроллеров.

Запускаются две копии эмулятора с одинаковым зерном случайности и интенсивностью: в первой светофорами
управляет центр, во второй работают штатные программы. Всё считается по «истине» эмулятора, а не по
распознаванию системы:
- пропускная способность объекта — сколько машин проехало через перекрёсток/переход, авт/ч;
- задержка машин — среднее число машин, стоящих в очередях на подходах (по /truth);
- ожидание пешеходов — время каждого пешехода от подхода к переходу до выхода на него (по живой ленте);
- переходы на красный — доля пешеходов, пошедших на запрещающий сигнал.

Запуск: python tools/compare_modes.py --smart http://127.0.0.1:8201 --standard http://127.0.0.1:8202 \\
            --minutes 15 --label "Час пик" [--out reports/modes_comparison]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import threading
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parent.parent
PED_WAIT, PED_CROSS = 1, 2
NODE_R = 18.0  # машина засчитана объекту, если прошла ближе этого к его центру, м


def get(url: str):
    return json.load(urllib.request.urlopen(url, timeout=20))


class Recorder:
    """Собирает показатели одной копии эмулятора."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        m = get(self.base + "/api/map")
        self.nodes = {s["object_id"]: (s["x"], s["y"]) for s in m["signals"] if s["object_id"]}
        self.titles = {o["id"]: o["title"] for o in get(self.base + "/api/objects")}
        self.peds: dict[int, dict] = {}
        self.waits: list[float] = []
        self.violations = 0
        self.served = 0
        self.cars_at: dict[str, set[int]] = {oid: set() for oid in self.nodes}
        self.queue_sum: dict[str, float] = {oid: 0.0 for oid in self.nodes}
        self.queue_n = 0
        self.t0: float | None = None
        self.t: float = 0.0
        self.exited0 = get(self.base + "/api/info").get("cars_exited", 0)
        self.stop = threading.Event()
        self.modes: dict[str, set[str]] = {oid: set() for oid in self.nodes}

    def _live(self) -> None:
        url = self.base.replace("http", "ws", 1) + "/ws/live"
        with connect(url, max_size=None) as ws:
            while not self.stop.is_set():
                d = json.loads(ws.recv())
                t = d["t"]
                if self.t0 is None:
                    self.t0 = t
                self.t = t
                for pid, x, y, st, viol in d["peds"]:
                    p = self.peds.setdefault(pid, {"state": st, "wait_from": None, "done": False, "viol": 0})
                    if st == PED_WAIT and p["wait_from"] is None and not p["done"]:
                        p["wait_from"] = t
                    if st == PED_CROSS and not p["done"]:
                        p["done"] = True
                        wait = t - p["wait_from"] if p["wait_from"] is not None else 0.0
                        self.waits.append(wait)
                        self.served += 1
                        p["viol"] = viol
                    if p["done"] and viol and not p["viol"]:
                        p["viol"] = 1
                    p["state"] = st
                for cid, x, y, _h, _k in d["cars"]:
                    for oid, (nx, ny) in self.nodes.items():
                        if abs(x - nx) < NODE_R and abs(y - ny) < NODE_R and math.hypot(x - nx, y - ny) < NODE_R:
                            self.cars_at[oid].add(cid)

    def _truth(self) -> None:
        while not self.stop.is_set():
            for oid in self.nodes:
                try:
                    tr = get(f"{self.base}/api/objects/{oid}/truth")
                    self.queue_sum[oid] += sum(a["queue"] for a in tr["approaches"])
                    mode = get(f"{self.base}/api/objects/{oid}")["signal"]["mode"]
                    self.modes[oid].add(mode)
                except Exception:
                    pass
            self.queue_n += 1
            self.stop.wait(5.0)

    def start(self) -> None:
        threading.Thread(target=self._live, daemon=True).start()
        threading.Thread(target=self._truth, daemon=True).start()

    def result(self) -> dict:
        hours = max(1e-6, (self.t - (self.t0 or self.t)) / 3600)
        viol = sum(1 for p in self.peds.values() if p["done"] and p["viol"])
        waits = sorted(self.waits)
        per = {}
        for oid in self.nodes:
            per[oid] = {"title": self.titles.get(oid, oid), "throughput_vph": round(len(self.cars_at[oid]) / hours),
                        "avg_queue": round(self.queue_sum[oid] / max(1, self.queue_n), 2),
                        "modes": sorted(self.modes[oid])}
        return {
            "sim_minutes": round(hours * 60, 1),
            "throughput_vph_total": round(sum(len(v) for v in self.cars_at.values()) / hours),
            "cars_exited_vph": round((get(self.base + "/api/info").get("cars_exited", 0) - self.exited0) / hours),
            "avg_queue_total": round(sum(self.queue_sum.values()) / max(1, self.queue_n), 1),
            "peds_served": self.served,
            "ped_wait_avg_s": round(statistics.fmean(waits), 1) if waits else None,
            "ped_wait_p90_s": round(waits[int(0.9 * (len(waits) - 1))], 1) if waits else None,
            "ped_wait_max_s": round(waits[-1], 1) if waits else None,
            "red_crossing_share": round(viol / max(1, self.served), 4),
            "objects": per,
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smart", required=True, help="эмулятор, которым управляет центр")
    ap.add_argument("--standard", required=True, help="эмулятор со штатными программами")
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default=str(ROOT / "reports" / "modes_comparison"))
    args = ap.parse_args()
    recs = {"smart": Recorder(args.smart), "standard": Recorder(args.standard)}
    for r in recs.values():
        r.start()
    t_end = time.time() + args.minutes * 60
    while time.time() < t_end:
        left = t_end - time.time()
        print(f"\r{args.label}: осталось {left / 60:4.1f} мин, пешеходов: умный {recs['smart'].served}, "
              f"штатный {recs['standard'].served}", end="", flush=True)
        time.sleep(5)
    for r in recs.values():
        r.stop.set()
    print()
    res = {"label": args.label, "smart": recs["smart"].result(), "standard": recs["standard"].result()}
    out = Path(args.out + (f"_{args.label}" if args.label else "") + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    s, b = res["smart"], res["standard"]
    print(f"{'':32} {'умный':>10} {'штатный':>10}")
    for k, name in [("throughput_vph_total", "Пропуск машин (сумма объектов), авт/ч"), ("cars_exited_vph", "Проехало участок, авт/ч"),
                    ("avg_queue_total", "Машин в очередях (среднее)"), ("peds_served", "Пешеходов перешло"),
                    ("ped_wait_avg_s", "Ожидание пешехода, с"), ("ped_wait_p90_s", "Ожидание, 90-й перцентиль, с"),
                    ("red_crossing_share", "Переходы на красный")]:
        print(f"{name:32} {s[k]!s:>10} {b[k]!s:>10}")
    print("->", out)


if __name__ == "__main__":
    main()
