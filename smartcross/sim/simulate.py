"""Microscopic-ish simulation of a signalized pedestrian crossing.

Compares control strategies on identical random traffic (same seed):
  * fixed   — жёсткий цикл (как на большинстве переходов);
  * button  — вызывное устройство: пешеходная фаза по кнопке после фиксированного
              минимального зелёного ТС; часть пешеходов кнопку не нажимает / кнопка неисправна;
  * smart   — предлагаемая система: видеодетекция + адаптивная логика (тот же Controller, что в проде),
              с моделированием ошибок детектора.

Models:
  vehicles — Poisson arrivals, point queue per crossing discharged at saturation flow
             (with start-up lost time) while vehicles have green;
  pedestrians — Poisson arrivals, plus group arrivals (e.g. a tram/bus stop nearby);
             each has a patience time; after it runs out a pedestrian crosses on red (violation).
"""
from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from smartcross.config import Config
from smartcross.controller.fsm import Controller, Mode, Observation, Phase, VehSignal
from smartcross.traffic.flow import FlowEstimator

DT = 0.5
STARTUP_LOST_S = 2.0
APPROACH_TRAVEL_S = 3.5          # time a free-flowing car spends in the approach zone (≈40 m at 40 km/h)
VEH_OCCUPANCY = 1.3              # people per car, for the person-delay criterion


@dataclass
class Scenario:
    name: str
    title: str
    veh_vph: float               # total vehicle flow, both directions
    ped_ph: float                # individual pedestrian arrivals per hour
    group_ph: float = 0.0        # group arrivals per hour
    group_size: tuple[int, int] = (5, 12)
    hours: float = 1.0


SCENARIOS = [
    Scenario("night", "Ночь: пустая дорога", veh_vph=60, ped_ph=20),
    Scenario("low", "День, низкая интенсивность", veh_vph=400, ped_ph=120),
    Scenario("medium", "День, средняя интенсивность", veh_vph=1200, ped_ph=200, group_ph=4),
    Scenario("rush", "Час пик + группы (остановка)", veh_vph=2600, ped_ph=300, group_ph=12),
]


@dataclass
class Ped:
    arrive: float
    side: str
    patience: float
    pressed: bool


@dataclass
class Result:
    strategy: str
    scenario: str
    peds: int = 0
    served: int = 0
    violations: int = 0
    ped_wait_avg: float = 0.0
    ped_wait_p95: float = 0.0
    veh: int = 0
    veh_delay_avg: float = 0.0
    veh_stops_share: float = 0.0
    ped_phases_ph: float = 0.0
    empty_ped_phases: int = 0
    person_delay_h: float = 0.0  # person-hours of delay per hour: pedestrians + car occupants
    waits: list[float] = field(default_factory=list, repr=False)


class ButtonController(Controller):
    """Classic push-button crossing: pedestrian phase after the button, once vehicles
    have had a fixed minimum green. No knowledge of traffic or of pedestrian count."""

    BUTTON_MIN_GREEN_S = 30.0

    def _on_veh_green(self, obs: Observation) -> None:
        if self.demand_since is not None and self.time_in_phase() >= self.BUTTON_MIN_GREEN_S:
            self._start_ped_cycle("кнопка")


def make_controller(strategy: str, cfg: Config) -> Controller:
    if strategy == "fixed":
        return Controller(cfg, mode=Mode.FIXED)
    if strategy == "button":
        return ButtonController(cfg, mode=Mode.ADAPTIVE)
    return Controller(cfg, mode=Mode.ADAPTIVE)


def simulate(strategy: str, sc: Scenario, cfg: Config | None = None, seed: int = 42,
             p_press: float = 0.75, det_miss: float = 0.1, veh_miss: float = 0.05) -> Result:
    cfg = cfg or Config()
    rnd = random.Random(seed)             # traffic: identical across strategies
    noise = random.Random(seed + 1000)    # sensor noise
    ctrl = make_controller(strategy, cfg)
    flow = FlowEstimator(cfg.flow_window)
    # warm-up estimator: the real system has history
    flow.flow = sc.veh_vph
    cap_per_s = cfg.site.lanes_total * cfg.control.saturation_flow_vphpl / 3600.0
    walk_s = cfg.site.crossing_length_m / cfg.site.ped_speed_mps

    horizon = sc.hours * 3600
    t = 0.0
    queue = 0.0                   # vehicles queued (fractional discharge)
    queue_arrivals: list[float] = []   # arrival times of queued vehicles (FIFO)
    recent: list[float] = []      # free-flow cars in the approach zone
    waiting: list[Ped] = []
    crossing: list[float] = []    # finish times of pedestrians on the crossing
    res = Result(strategy, sc.name)
    veh_delay_sum = 0.0
    viol_wait = 0.0
    stopped = 0
    discharge_acc = 0.0
    green_since: float | None = None
    served_in_phase = 0
    prev_phase = ctrl.state.phase

    next_veh = rnd.expovariate(sc.veh_vph / 3600) if sc.veh_vph else math.inf
    next_ped = rnd.expovariate(sc.ped_ph / 3600) if sc.ped_ph else math.inf
    next_grp = rnd.expovariate(sc.group_ph / 3600) if sc.group_ph else math.inf

    def new_ped(at: float) -> Ped:
        # patience: lognormal, median ~45 s (field studies report 30-60 s)
        return Ped(at, rnd.choice("AB"), rnd.lognormvariate(math.log(45), 0.5), rnd.random() < p_press)

    while t < horizon:
        # ---------------- arrivals
        while next_veh <= t:
            res.veh += 1
            veh_green = ctrl.state.veh in (VehSignal.GREEN, VehSignal.GREEN_BLINK)
            if veh_green and queue < 0.5:
                recent.append(next_veh)
            else:
                queue += 1
                queue_arrivals.append(next_veh)
                stopped += 1
            if noise.random() > veh_miss:
                flow.add_vehicle(next_veh)
            next_veh += rnd.expovariate(sc.veh_vph / 3600)
        while next_ped <= t:
            waiting.append(new_ped(next_ped))
            res.peds += 1
            next_ped += rnd.expovariate(sc.ped_ph / 3600)
        while next_grp <= t:
            n = rnd.randint(*sc.group_size)
            side = rnd.choice("AB")
            for _ in range(n):
                p = new_ped(next_grp + rnd.uniform(0, 5))
                p.side = side
                waiting.append(p)
            res.peds += n
            next_grp += rnd.expovariate(sc.group_ph / 3600)
        recent = [a for a in recent if t - a < APPROACH_TRAVEL_S]
        crossing = [f for f in crossing if f > t]

        # ---------------- sensing
        present = [p for p in waiting if p.arrive <= t]
        if strategy == "smart":
            seen = {"A": 0, "B": 0}
            for p in present:
                if noise.random() > det_miss:
                    seen[p.side] += 1
            obs = Observation(ped_waiting=seen, ped_on_crossing=len(crossing),
                              veh_in_approach=int(queue) + len(recent),
                              veh_flow_vph=flow.update(t).flow_vph)
        else:
            obs = Observation(ped_waiting={}, button=any(p.pressed for p in present),
                              ped_vision_ok=False, veh_vision_ok=False, veh_flow_vph=sc.veh_vph)
        state = ctrl.tick(t, obs)

        # ---------------- pedestrians
        if state.phase == Phase.PED_GREEN:
            for p in present:
                res.waits.append(t - p.arrive)
                crossing.append(t + walk_s)
                served_in_phase += 1
            res.served += len(present)
            waiting = [p for p in waiting if p.arrive > t]
        else:
            # impatient pedestrians cross on red (only if they've actually been waiting)
            keep = []
            for p in waiting:
                if p.arrive <= t and t - p.arrive > p.patience:
                    res.violations += 1
                    viol_wait += t - p.arrive
                else:
                    keep.append(p)
            waiting = keep
        if prev_phase != state.phase:
            if prev_phase == Phase.PED_GREEN_BLINK:
                if served_in_phase == 0:
                    res.empty_ped_phases += 1
                served_in_phase = 0
                res.ped_phases_ph += 1
            prev_phase = state.phase

        # ---------------- vehicles
        if state.veh in (VehSignal.GREEN, VehSignal.GREEN_BLINK):
            green_since = t if green_since is None else green_since
            if t - green_since >= STARTUP_LOST_S:
                discharge_acc += cap_per_s * DT
                while discharge_acc >= 1 and queue_arrivals:
                    discharge_acc -= 1
                    queue -= 1
                    veh_delay_sum += t - queue_arrivals.pop(0)
                if not queue_arrivals:
                    discharge_acc = 0.0
        else:
            green_since = None
            discharge_acc = 0.0
        t += DT

    veh_delay_sum += sum(horizon - a for a in queue_arrivals)
    if res.waits:
        w = sorted(res.waits)
        res.ped_wait_avg = round(sum(w) / len(w), 1)
        res.ped_wait_p95 = round(w[int(0.95 * (len(w) - 1))], 1)
    res.veh_delay_avg = round(veh_delay_sum / max(res.veh, 1), 1)
    res.veh_stops_share = round(stopped / max(res.veh, 1), 3)
    res.ped_phases_ph = round(res.ped_phases_ph / sc.hours, 1)
    res.person_delay_h = round((sum(res.waits) + viol_wait + VEH_OCCUPANCY * veh_delay_sum) / 3600 / sc.hours, 2)
    return res


STRATEGIES = {"fixed": "Фиксированный цикл", "button": "Кнопка вызова", "smart": "SmartCross (видео)"}


def run_all(hours: float = 1.0, seeds: int = 5) -> list[dict]:
    rows = []
    for sc in SCENARIOS:
        sc = Scenario(**{**asdict(sc), "hours": hours})
        for st in STRATEGIES:
            rs = [simulate(st, sc, seed=s) for s in range(seeds)]
            avg = lambda k: round(sum(getattr(r, k) for r in rs) / len(rs), 2)
            rows.append({
                "scenario": sc.name, "scenario_title": sc.title, "strategy": st,
                "ped_wait_avg": avg("ped_wait_avg"), "ped_wait_p95": avg("ped_wait_p95"),
                "violation_share": round(sum(r.violations for r in rs) / max(sum(r.peds for r in rs), 1), 3),
                "veh_delay_avg": avg("veh_delay_avg"), "veh_stops_share": avg("veh_stops_share"),
                "ped_phases_ph": avg("ped_phases_ph"), "person_delay_h": avg("person_delay_h"), "empty_ped_phases_ph": round(avg("empty_ped_phases") / hours, 1),
                "peds": sum(r.peds for r in rs), "veh": sum(r.veh for r in rs),
            })
    return rows


def to_markdown(rows: list[dict]) -> str:
    out = ["| Сценарий | Стратегия | Ожидание пешехода, с (ср / 95%) | Переходят на красный | "
           "Задержка ТС, с/авт | Доля остановленных ТС | Суммарная задержка людей, чел·ч/ч | Пеш. фаз в час (пустых) |",
           "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['scenario_title']} | {STRATEGIES[r['strategy']]} | {r['ped_wait_avg']:.1f} / {r['ped_wait_p95']:.1f} | "
                   f"{100 * r['violation_share']:.1f}% | {r['veh_delay_avg']:.1f} | {100 * r['veh_stops_share']:.0f}% | {r['person_delay_h']:.2f} | "
                   f"{r['ped_phases_ph']:.0f} ({r['empty_ped_phases_ph']:.0f}) |")
    return "\n".join(out)


def plot(rows: list[dict], out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    scen = list(dict.fromkeys(r["scenario_title"] for r in rows))
    colors = {"fixed": "#9aa5b1", "button": "#f0b429", "smart": "#2fbf71"}
    metrics = [("ped_wait_avg", "Среднее ожидание пешехода, с"), ("violation_share", "Доля переходов на красный"),
               ("veh_delay_avg", "Средняя задержка ТС, с/авт"), ("person_delay_h", "Суммарная задержка людей, чел·ч / ч")]
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    axes = axes.ravel()
    x = np.arange(len(scen))
    for ax, (key, title) in zip(axes, metrics):
        for i, st in enumerate(STRATEGIES):
            vals = [next(r[key] for r in rows if r["scenario_title"] == s and r["strategy"] == st) for s in scen]
            ax.bar(x + (i - 1) * 0.27, vals, 0.27, label=STRATEGIES[st], color=colors[st])
        ax.set_title(title)
        ax.set_xticks(x, [s.replace(": ", ":\n").replace(", ", ",\n") for s in scen], fontsize=8)
        ax.grid(axis="y", alpha=.3)
        if key == "violation_share":
            ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)


def pareto(hours: float, seeds: int, out: Path) -> list[dict]:
    """Trade-off between total person delay and red-light crossings: SmartCross for several
    pedestrian time weights vs the fixed cycle and the push button."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    weights = (0.75, 1.5, 3.0, 6.0, 12.0)
    pts = []
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    for ax, name in zip(axes, ("medium", "rush")):
        sc = next(s for s in SCENARIOS if s.name == name)
        sc = Scenario(**{**asdict(sc), "hours": hours})

        def point(strategy, cfg=None):
            rs = [simulate(strategy, sc, cfg=cfg, seed=s) for s in range(seeds)]
            return (sum(r.person_delay_h for r in rs) / seeds,
                    sum(r.violations for r in rs) / max(sum(r.peds for r in rs), 1))

        for st, color in (("fixed", "#9aa5b1"), ("button", "#f0b429")):
            d, v = point(st)
            ax.scatter([v], [d], s=120, color=color, label=STRATEGIES[st], zorder=3)
            pts.append({"scenario": name, "strategy": st, "person_delay_h": round(d, 2), "violation_share": round(v, 3)})
        xs, ys = [], []
        for w in weights:
            cfg = Config()
            cfg.control.ped_time_weight = w
            d, v = point("smart", cfg)
            xs.append(v)
            ys.append(d)
            ax.annotate(f"w={w:g}", (v, d), textcoords="offset points", xytext=(6, 4), fontsize=8)
            pts.append({"scenario": name, "strategy": f"smart_w{w:g}", "person_delay_h": round(d, 2),
                        "violation_share": round(v, 3)})
        ax.plot(xs, ys, "-o", color="#2fbf71", label="SmartCross (вес пешехода w)", zorder=2)
        ax.set_title(sc.title)
        ax.set_xlabel("Доля пешеходов, перешедших на красный")
        ax.set_ylabel("Суммарная задержка людей, чел·ч / ч")
        ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        ax.grid(alpha=.3)
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return pts


def main() -> None:
    ap = argparse.ArgumentParser(description="Сравнение стратегий управления переходом")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = run_all(args.hours, args.seeds)
    md = to_markdown(rows)
    (out / "simulation.md").write_text(
        f"# Результаты моделирования\n\n{args.seeds} прогонов по {args.hours:g} ч на сценарий, одинаковые "
        f"случайные потоки для всех стратегий.\n\n{md}\n\n![графики](simulation.png)\n", encoding="utf-8")
    (out / "simulation.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    plot(rows, out / "simulation.png")
    pts = pareto(args.hours, args.seeds, out / "pareto.png")
    (out / "pareto.json").write_text(json.dumps(pts, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(out / "simulation.md", "a", encoding="utf-8") as f:
        f.write("\n## Компромисс «задержка ↔ безопасность»\n\n![pareto](pareto.png)\n")
    print(md)


if __name__ == "__main__":
    main()
