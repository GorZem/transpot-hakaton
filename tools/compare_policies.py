"""Сравнение алгоритмов управления на модели улицы.

Политики:
- fixed — фиксированный цикл (как у обычного светофора), пешеходы обслуживаются каждый цикл;
- rules — прежние правила (задержка D по интенсивности, разрыв в потоке, порог группы);
- cost  — минимум суммарной задержки людей (текущий алгоритм системы).

Для каждого типа объекта и сценария — несколько прогонов по часу с одинаковыми потоками
для всех политик. Результат: reports/policy_comparison.md и .json.
Запуск: python tools/compare_policies.py [--minutes 60] [--seeds 3]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from node.control import Controller  # noqa: E402
from node.flow import FlowEstimator  # noqa: E402
from node.model import Mode, build_layout  # noqa: E402
from node.params import Params  # noqa: E402
from sim_world import World  # noqa: E402

SITES = {s["id"]: s for s in json.loads((ROOT / "data" / "sites.json").read_text(encoding="utf-8"))["sites"]}
OBJECTS = {"crossing": "p-krasnodonskaya-mid", "tee": "t-novorossiyskaya-stavropolskaya",
           "cross": "x-krasnodarskaya-krasnodonskaya"}
SCENARIOS = {"Ночь (03:00)": (3.0, 1.0), "День (12:00)": (12.0, 1.0), "Час пик (08:30)": (8.5, 1.0),
             "Час пик +30 %": (8.5, 1.3)}
POLICIES = {"fixed": "Фиксированный цикл", "rules": "Прежние правила", "cost": "Минимум задержки людей"}
EVAL_OCCUPANCY = 1.3        # людей в машине — для метрики «задержка на человека»
EVAL_STOP_PENALTY_S = 4.0   # потери на торможение и разгон — для той же метрики
KIND_TITLES = {"crossing": "Переход", "tee": "Т-образный", "cross": "Крестовой"}


def demand(layout) -> tuple[dict, dict]:
    """Пиковая интенсивность по группам и поток пешеходов (на переход, чел/мин)."""
    veh = {}
    for g in layout.veh_groups():
        if layout.kind == "crossing":
            veh[g] = 1000        # оба направления
        elif layout.kind == "tee" and g == "veh_B":
            veh[g] = 350         # примыкание
        else:
            veh[g] = 900 if g == "veh_A" else 650
    ped = {g: 4.0 if layout.kind == "crossing" else 2.5 for g in layout.ped_groups()}
    return veh, ped


def run(kind: str, policy: str, hour: float, volume: float, seed: int, minutes: float, dt: float = 0.1,
        overrides: dict | None = None) -> dict:
    site = SITES[OBJECTS[kind]]
    L = build_layout(site)
    p = Params(**(overrides or {}))
    veh, ped = demand(L)
    world = World(L, veh, ped, seed)
    world.volume = volume
    ctrl = Controller(L, p, 0.0, policy="rules" if policy == "rules" else "cost")
    if policy == "fixed":
        ctrl.set_mode(Mode.FIXED, "сравнение", 0.0)
    flows = {g: FlowEstimator(0.0) for g in L.veh_groups()}
    sig = dict(ctrl.signals)
    warm = 300.0
    waits, viol, passed, stopped, queue_s = [], 0, 0, 0, 0.0
    n = int((minutes * 60 + warm) / dt)
    for k in range(n):
        now = k * dt
        m = world.step(now, dt, hour, sig)
        for g, c in m.passed.items():
            flows[g].add(now, c)
        obs = world.observe(now)
        for g in L.veh_groups():
            obs.flow_vph[g] = flows[g].estimate(now, p)
        sig = ctrl.tick(now, obs)
        if now < warm:
            continue
        for _g, w, red in m.served:
            waits.append(w)
            viol += red
        passed += sum(m.passed.values())
        stopped += sum(m.stopped.values())
        queue_s += m.veh_queue_s
    # метрика одинакова для всех вариантов и не зависит от подбираемых параметров
    occ = EVAL_OCCUPANCY
    people = len(waits) + passed * occ
    person_delay = sum(waits) + occ * (queue_s + stopped * EVAL_STOP_PENALTY_S)
    waits.sort()
    return {
        "ped": len(waits),
        "ped_wait": statistics.fmean(waits) if waits else 0.0,
        "ped_p95": waits[int(0.95 * (len(waits) - 1))] if waits else 0.0,
        "viol": viol / len(waits) if waits else 0.0,
        "veh": passed,
        "veh_delay": queue_s / passed if passed else 0.0,
        "stops": stopped / passed if passed else 0.0,
        "person_delay": person_delay / people if people else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--seeds", type=int, default=3)
    args = ap.parse_args()
    t0 = time.time()
    results = {}
    for kind in OBJECTS:
        for scen, (hour, vol) in SCENARIOS.items():
            for pol in POLICIES:
                runs = [run(kind, pol, hour, vol, seed, args.minutes) for seed in range(args.seeds)]
                avg = {k: statistics.fmean(r[k] for r in runs) for k in runs[0]}
                results[f"{kind}|{scen}|{pol}"] = avg
                print(f"{KIND_TITLES[kind]:10} {scen:16} {POLICIES[pol]:24} ожидание {avg['ped_wait']:5.1f} с "
                      f"(95 % ≤ {avg['ped_p95']:4.0f}) на красный {avg['viol'] * 100:4.1f} %  "
                      f"задержка машины {avg['veh_delay']:4.1f} с  остановок {avg['stops'] * 100:3.0f} %  "
                      f"на человека {avg['person_delay']:5.1f} с", flush=True)
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / "policy_comparison.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# Сравнение алгоритмов управления", "",
             f"Модель улицы (tools/sim_world.py), {args.seeds} прогона по {args.minutes:.0f} мин на каждое сочетание, "
             "одинаковые потоки для всех политик. «Задержка на человека» — средняя задержка всех людей: "
             f"пешеходов и пассажиров машин (1,3 чел. в машине, остановка = +{EVAL_STOP_PENALTY_S:.0f} с).", ""]
    for kind in OBJECTS:
        lines += [f"## {KIND_TITLES[kind]} ({OBJECTS[kind]})", "",
                  "| Сценарий | Алгоритм | Ожидание пешехода, с | 95 % ждут не дольше, с | На красный | "
                  "Задержка машины, с | Остановок | Задержка на человека, с |",
                  "|---|---|---|---|---|---|---|---|"]
        for scen in SCENARIOS:
            for pol in POLICIES:
                r = results[f"{kind}|{scen}|{pol}"]
                b = "**" if pol == "cost" else ""
                lines.append(f"| {scen} | {b}{POLICIES[pol]}{b} | {r['ped_wait']:.1f} | {r['ped_p95']:.0f} | "
                             f"{r['viol'] * 100:.1f} % | {r['veh_delay']:.1f} | {r['stops'] * 100:.0f} % | "
                             f"{b}{r['person_delay']:.1f}{b} |")
        lines.append("")
    (out / "policy_comparison.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"-> reports/policy_comparison.md ({time.time() - t0:.0f} с)")


if __name__ == "__main__":
    main()
