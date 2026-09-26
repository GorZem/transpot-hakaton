"""Parameter sweep for the SmartCross decision rule on the simulator.

Objective: total person delay (pedestrians + vehicle occupants), with the share of
pedestrians crossing on red as a safety constraint. Usage: python scripts/tune.py
"""
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smartcross.config import Config  # noqa: E402
from smartcross.sim.simulate import SCENARIOS, simulate  # noqa: E402

SEEDS = 3


def evaluate(cfg: Config, strategy: str = "smart") -> dict:
    out = {}
    for sc in SCENARIOS:
        rs = [simulate(strategy, sc, cfg=cfg, seed=s) for s in range(SEEDS)]
        out[sc.name] = (sum(r.person_delay_h for r in rs) / SEEDS,
                        sum(r.violations for r in rs) / max(sum(r.peds for r in rs), 1))
    return out


def fmt(res: dict) -> str:
    return "  ".join(f"{k}: {d:5.2f} ч·ч/{100 * v:4.1f}%" for k, (d, v) in res.items())


if __name__ == "__main__":
    base = Config()
    for st in ("fixed", "button"):
        print(f"{st:>28}  {fmt(evaluate(base, st))}")
    for w, tau, gf in itertools.product((1.5, 3), (10, 20), (0.5, 1.0)):
        cfg = base.model_copy(deep=True)
        cfg.control.ped_time_weight, cfg.control.ped_impatience_s, cfg.timing.gap_out_factor = w, tau, gf
        print(f"w={w:<4} tau={tau:<3} gap={gf:<4}  {fmt(evaluate(cfg))}", flush=True)
