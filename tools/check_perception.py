"""Проверка распознавания: система против «истины» эмулятора.

Раз в интервал берёт у центра наблюдение объекта (сколько ждут у переходов, очереди на подходах)
и у эмулятора фактическую обстановку, и считает ошибки. Эмулятор знает истину только потому,
что это модель; в реальной эксплуатации такую проверку делают ручным подсчётом по записи.

Запуск (центр и эмулятор работают): python tools/check_perception.py [--samples 60] [--every 2]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request


def get(url: str):
    return json.load(urllib.request.urlopen(url, timeout=10))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--center", default="http://127.0.0.1:8000")
    ap.add_argument("--emulator", default="http://127.0.0.1:8100")
    ap.add_argument("--samples", type=int, default=60)
    ap.add_argument("--every", type=float, default=2.0)
    args = ap.parse_args()
    sites = [s for s in get(args.center + "/api/overview")["sites"] if s["equipped"]]
    err = {"ped": [], "veh": [], "ped_present": [0, 0], "queue_present": [0, 0]}
    per_site: dict[str, list[float]] = {s["id"]: [] for s in sites}
    for k in range(args.samples):
        for s in sites:
            st = get(f"{args.center}/api/sites/{s['id']}/state")
            if not st["engaged"]:
                continue
            tr = get(f"{args.emulator}/api/objects/{s['id']}/truth")
            ped_truth: dict[str, int] = {}
            for cw in tr["crosswalks"]:
                ped_truth[cw["group"]] = ped_truth.get(cw["group"], 0) + cw["waiting_side_0"] + cw["waiting_side_1"]
            q_truth: dict[str, int] = {}
            for a in tr["approaches"]:
                g = "veh" if s["kind"] == "crossing" else f"veh_{a['axis']}"
                q_truth[g] = q_truth.get(g, 0) + a["queue"]
            obs = st["observation"]
            for g, n in ped_truth.items():
                seen = obs["waiting"].get(g)
                if seen is None:
                    continue
                err["ped"].append(abs(seen - n))
                per_site[s["id"]].append(abs(seen - n))
                if n > 0:
                    err["ped_present"][0] += 1
                    err["ped_present"][1] += int(seen > 0)
            for g, n in q_truth.items():
                seen = obs["queue"].get(g)
                if seen is None:
                    continue
                err["veh"].append(abs(seen - n))
                if n > 0:
                    err["queue_present"][0] += 1
                    err["queue_present"][1] += int(seen > 0)
        print(f"\rзамер {k + 1}/{args.samples}", end="", flush=True)
        time.sleep(args.every)
    mean = lambda v: sum(v) / len(v) if v else float("nan")
    print()
    print(f"Ждущие пешеходы: средняя ошибка {mean(err['ped']):.2f} чел. на группу переходов, "
          f"люди замечены в {err['ped_present'][1]}/{err['ped_present'][0]} случаях, когда они были")
    print(f"Очередь машин: средняя ошибка {mean(err['veh']):.2f} авт. на подход, "
          f"очередь замечена в {err['queue_present'][1]}/{err['queue_present'][0]} случаях, когда она была")
    for sid, v in per_site.items():
        print(f"  {sid:36} ошибка по ждущим {mean(v):.2f}")


if __name__ == "__main__":
    main()
