"""Контроллер фаз объекта.

Механика общая для всех типов: объект обслуживает фазы (стадии) по кругу, между ними идёт
переходный интервал (зелёный мигающий → жёлтый → все красные → красный с жёлтым).
Решение, когда закончить текущую фазу и какую включить следующей, зависит от типа объекта:
- переход: задержка включения пешеходной фазы по плотности потока, разрыв, группа, предел ожидания;
- перекрёсток: продление по подходу машин, пропуск фаз без спроса, приоритет пешеходов и спецтранспорта.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from node.model import MODE_TITLES, Layout, Mode, Observation, Ped, Veh
from node.params import Params

EPS = 1e-6  # сравнения времени с запасом на погрешность float


@dataclass
class Transition:
    to: int
    t0: float
    losing_veh: set[str]
    losing_ped: set[str]
    gaining_veh: set[str]
    reason: str
    red_at: float | None = None      # когда все уходящие группы получили красный
    ar_end: float | None = None      # когда закончились «все красные»
    extended: bool = False


@dataclass
class Decision:
    go: bool
    to: int | None = None
    reason: str = ""
    status: str = ""


@dataclass
class ControllerEvent:
    level: str   # info | warn | critical
    kind: str
    message: str
    data: dict = field(default_factory=dict)


class Controller:
    def __init__(self, layout: Layout, params: Params, now: float = 0.0):
        self.L = layout
        self.p = params
        self.mode = Mode.ADAPTIVE
        self.mode_reason = "все камеры исправны"
        self.stage = 0
        self.stage_t0 = now
        self.signals: dict[str, str] = {g: (Veh.RED if x.kind == "veh" else Ped.RED) for g, x in layout.groups.items()}
        self.ped_active: set[str] = set()
        self.ped_end: dict[str, float] = {}
        self.last_served: dict[str, float] = {g: now for g in layout.ped_groups()}
        self.status = ""
        self.info: dict = {}
        self.events: list[ControllerEvent] = []
        self.trans: Transition | None = Transition(0, now, set(), set(), set(layout.stages[0].veh), "запуск объекта")

    # ---------- внешнее управление ----------
    def set_mode(self, mode: Mode, reason: str, now: float) -> None:
        if mode == self.mode:
            self.mode_reason = reason
            return
        prev = self.mode
        self.mode, self.mode_reason = mode, reason
        level = "critical" if mode == Mode.FLASHING else ("warn" if mode in (Mode.DEGRADED, Mode.FIXED) else "info")
        self._event(level, "mode", f"Режим «{MODE_TITLES[mode].lower()}»: {reason}", mode=mode.value)
        if mode == Mode.FLASHING:
            self.trans = None
            self.ped_active.clear()
            for g, x in self.L.groups.items():
                self.signals[g] = Veh.FLASH if x.kind == "veh" else Ped.OFF
        elif prev == Mode.FLASHING:
            for g, x in self.L.groups.items():
                self.signals[g] = Veh.RED if x.kind == "veh" else Ped.RED
            self.stage = 0
            self.trans = Transition(0, now, set(), set(), set(self.L.stages[0].veh), "выход из жёлтого мигающего")

    def adopt(self, stage: int, ped_green: set[str], now: float) -> None:
        """Подхватить управление с устойчивой фазы, в которой объект уже работает по своей программе."""
        self.trans = None
        self.stage, self.stage_t0 = stage, now
        st = self.L.stages[stage]
        for g, x in self.L.groups.items():
            if x.kind == "veh":
                self.signals[g] = Veh.GREEN if g in st.veh else Veh.RED
            else:
                self.signals[g] = Ped.GREEN if g in ped_green else Ped.RED
        self.ped_active = set(ped_green)
        self.ped_end = {g: now + self.p.ped_min_green_s for g in ped_green}
        for g in ped_green:
            self.last_served[g] = now
        self._event("info", "takeover", f"Управление принято на фазе «{st.title}»")

    def drain_events(self) -> list[ControllerEvent]:
        ev, self.events = self.events, []
        return ev

    # ---------- такт ----------
    def tick(self, now: float, obs: Observation) -> dict[str, str]:
        if self.mode == Mode.FLASHING:
            self.status = "Объект не регулируется: жёлтый мигающий"
        elif self.trans is not None:
            self._run_transition(now, obs)
        else:
            self._run_stage(now, obs)
        return dict(self.signals)

    # ---------- переходный интервал ----------
    def _run_transition(self, now: float, obs: Observation) -> None:
        tr, p = self.trans, self.p
        e = now - tr.t0
        blink = max(p.green_blink_s if tr.losing_veh else 0, p.ped_blink_s if tr.losing_ped else 0)
        yellow = p.yellow_s if tr.losing_veh else 0
        if e < blink - EPS:
            for g in tr.losing_veh:
                self.signals[g] = Veh.GREEN_BLINK
            for g in tr.losing_ped:
                self.signals[g] = Ped.GREEN_BLINK
            self.status = "Смена фазы: зелёный мигает"
            return
        for g in tr.losing_ped:
            self.signals[g] = Ped.RED
        if e < blink + yellow - EPS:
            for g in tr.losing_veh:
                self.signals[g] = Veh.YELLOW
            self.status = "Смена фазы: жёлтый"
            return
        for g in tr.losing_veh:
            self.signals[g] = Veh.RED
        if tr.red_at is None:
            tr.red_at = now
        if tr.ar_end is None:
            base_end = tr.red_at + self.p.all_red_s
            if now < base_end - EPS:
                self.status = "Все красные"
                return
            # Продление «все красные», пока пешеходы заканчивают переход через путь машин следующей фазы.
            on_x = self._peds_in_conflict(obs, tr.gaining_veh)
            if on_x and self.mode in (Mode.ADAPTIVE, Mode.DEGRADED) and now < base_end + p.clearance_ext_max_s - EPS:
                if not tr.extended:
                    tr.extended = True
                    self._event("info", "clearance", "Продление «все красные»: пешеходы ещё на переходе")
                self.status = "Продление: пешеходы заканчивают переход"
                return
            tr.ar_end = now
        if tr.gaining_veh and now < tr.ar_end + p.red_yellow_s - EPS:
            for g in tr.gaining_veh:
                self.signals[g] = Veh.RED_YELLOW
            self.status = "Красный с жёлтым"
            return
        self._start_stage(tr.to, now, obs, tr.reason)

    def _peds_in_conflict(self, obs: Observation, veh: set[str]) -> bool:
        for g in self.L.ped_groups():
            if any(v in self.L.conflicting(g) for v in veh) and (obs.on_crosswalk.get(g) or 0) > 0:
                return True
        return False

    # ---------- фаза ----------
    def _start_stage(self, idx: int, now: float, obs: Observation, reason: str) -> None:
        self.trans = None
        self.stage, self.stage_t0 = idx, now
        st = self.L.stages[idx]
        for g in st.veh:
            self.signals[g] = Veh.GREEN
        self.ped_active.clear()
        self.ped_end.clear()
        for g in st.ped:
            if st.ped_only or self.mode == Mode.FIXED or self._ped_demand(g, obs, now):
                self._activate_ped(g, now, obs)
        self._event("info", "stage", f"{st.title}: {reason}", stage=st.id)

    def _activate_ped(self, g: str, now: float, obs: Observation) -> None:
        self.signals[g] = Ped.GREEN
        self.ped_active.add(g)
        self.ped_end[g] = now + self.ped_green_duration(g, obs)
        self.last_served[g] = now

    def ped_green_duration(self, g: str, obs: Observation) -> float:
        p = self.p
        length = self.L.groups[g].length_m or 14.0
        base = max(p.ped_min_green_s, math.ceil(length / p.ped_speed_mps) - p.ped_blink_s)
        n = obs.waiting.get(g) or 0
        bonus = min(6, max(0, math.ceil((n - 4) / 3))) if self.mode == Mode.ADAPTIVE else 0
        return base + bonus

    def _ped_demand(self, g: str, obs: Observation, now: float) -> bool:
        n = obs.waiting.get(g)
        if n is None:  # зона не видна: плановый вызов
            return now - self.last_served[g] >= self.p.blind_call_s
        return n > 0

    def _run_stage(self, now: float, obs: Observation) -> None:
        st = self.L.stages[self.stage]
        t = now - self.stage_t0
        # Пешеходы, пришедшие во время фазы, получают зелёный, если фаза это позволяет.
        if not st.ped_only and self.mode != Mode.FIXED:
            for g in st.ped:
                if g not in self.ped_active and self._ped_demand(g, obs, now):
                    self._activate_ped(g, now, obs)
        earliest = max(self.ped_end.values(), default=now)
        if st.ped_only:
            left = earliest - now
            self.status = f"Пешеходная фаза, осталось {max(0, left):.0f} с"
            if now >= earliest - EPS:
                self._switch(self._next_stage(obs, now), "пешеходная фаза завершена", now, obs)
            return
        if self.mode == Mode.FIXED:
            d = self._decide_fixed(t)
        elif self.L.kind == "crossing":
            d = self._decide_crossing(now, obs, t)
        else:
            d = self._decide_intersection(now, obs, t)
        self.status = d.status or d.reason
        if d.go and now >= earliest - EPS:
            self._switch(d.to if d.to is not None else self._next_stage(obs, now), d.reason, now, obs)
        elif d.go:
            self.status = "Ждём окончания пешеходного зелёного"

    def _switch(self, to: int, reason: str, now: float, obs: Observation) -> None:
        cur, nxt = self.L.stages[self.stage], self.L.stages[to]
        losing_veh = set(cur.veh) - set(nxt.veh)
        losing_ped = set(self.ped_active) - set(nxt.ped)
        gaining_veh = set(nxt.veh) - set(cur.veh)
        self._event("info", "decision", reason, from_stage=cur.id, to_stage=nxt.id)
        self.trans = Transition(to, now, losing_veh, losing_ped, gaining_veh, reason)
        self.ped_active -= losing_ped
        self._run_transition(now, obs)  # мигание начинается в этом же такте

    def _next_stage(self, obs: Observation, now: float) -> int:
        n = len(self.L.stages)
        for k in range(1, n + 1):
            j = (self.stage + k) % n
            if j == self.stage or self.mode == Mode.FIXED or self._stage_demand(j, obs, now):
                return j
        return (self.stage + 1) % n

    def _stage_demand(self, j: int, obs: Observation, now: float) -> bool:
        st = self.L.stages[j]
        for g in st.veh:
            q, eta = obs.queue.get(g), obs.eta.get(g)
            if q is None or q > 0 or (eta is not None and eta < 15) or g in obs.emergency:
                return True
        return any(self._ped_demand(g, obs, now) for g in st.ped)

    # ---------- решения ----------
    def _decide_fixed(self, t: float) -> Decision:
        left = self.p.fixed_green_s - t
        return Decision(t >= self.p.fixed_green_s - EPS, reason="фиксированный план: время фазы истекло",
                        status=f"Фиксированный план, смена через {max(0, left):.0f} с")

    def flow_estimate(self, obs: Observation, groups) -> float | None:
        known = [obs.flow_vph[g] for g in groups if obs.flow_vph.get(g) is not None]
        if not known:
            return None
        return sum(known) * len(groups) / len(known)

    def _decide_crossing(self, now: float, obs: Observation, t: float) -> Decision:
        p, st = self.p, self.L.stages[self.stage]
        ped_stage = next(i for i, s in enumerate(self.L.stages) if s.ped_only)
        cw = self.L.stages[ped_stage].ped[0]
        q = self.flow_estimate(obs, st.veh) or 0.0
        blind = any(obs.flow_vph.get(g) is None for g in st.veh)
        gmin = p.veh_min_green_empty_s if q < p.empty_road_vph else p.veh_min_green_s
        D = p.delay_min_s + (p.delay_max_s - p.delay_min_s) * min(1.0, q / p.flow_sat_vph)
        n = obs.waiting.get(cw)
        wait = obs.max_wait.get(cw) or 0.0
        group = n is not None and n >= p.group_threshold
        d_eff = max(p.delay_min_s, D * p.group_factor) if group else D
        etas = [obs.eta.get(g) for g in st.veh]
        queues = [obs.queue.get(g) for g in st.veh]
        gap = (not blind and all(e is None or e > p.gap_s for e in etas) and all((x or 0) == 0 for x in queues))
        self.info = {"flow_vph": round(q), "delay_s": round(D, 1), "delay_eff_s": round(d_eff, 1),
                     "waiting": n, "max_wait_s": round(wait, 1), "gap": gap, "group": group, "min_green_s": gmin}
        if t < gmin:
            return Decision(False, status=f"Минимальный зелёный транспорту {gmin:.0f} с")
        if obs.emergency & set(st.veh):
            return Decision(False, status="Удержание: приближается спецтранспорт")
        if n is None:
            due = p.blind_call_s - (now - self.last_served[cw])
            return Decision(due <= 0, ped_stage, f"пешеходы не видны: плановый вызов раз в {p.blind_call_s:.0f} с",
                            f"Пешеходов не видно, плановый вызов через {max(0, due):.0f} с")
        if n == 0:
            return Decision(False, status="Пешеходов нет: зелёный остаётся транспорту")
        if gap and wait >= p.delay_min_s:
            return Decision(True, ped_stage, f"разрыв в потоке: ближайшая машина дальше {p.gap_s:.0f} с (ждали {wait:.0f} с)")
        if wait >= d_eff:
            if group:
                return Decision(True, ped_stage, f"группа {n} чел.: задержка сокращена до {d_eff:.0f} с")
            return Decision(True, ped_stage, f"задержка D = {D:.0f} с выдержана при потоке {q:.0f} авт/ч")
        if wait >= p.max_wait_s:
            return Decision(True, ped_stage, f"предельное ожидание {p.max_wait_s:.0f} с")
        return Decision(False, status=f"Ждут {n} чел., {wait:.0f} из {d_eff:.0f} с{' (группа)' if group else ''}")

    def _decide_intersection(self, now: float, obs: Observation, t: float) -> Decision:
        p, st = self.p, self.L.stages[self.stage]
        q_all = self.flow_estimate(obs, self.L.veh_groups())
        gmin = p.veh_min_green_empty_s if (q_all is not None and q_all < p.empty_road_vph) else p.veh_min_green_s
        others = [j for j in range(len(self.L.stages)) if j != self.stage and self._stage_demand(j, obs, now)]
        self.info = {"flow_vph": None if q_all is None else round(q_all), "min_green_s": gmin,
                     "demand": [self.L.stages[j].id for j in others], "green_s": round(t)}
        if t < gmin:
            return Decision(False, status=f"Минимальный зелёный {gmin:.0f} с")
        if obs.emergency & set(st.veh):
            if t < p.veh_max_green_s * 1.5:
                return Decision(False, status="Удержание: спецтранспорт на подходе")
        for j in others:
            if obs.emergency & set(self.L.stages[j].veh):
                return Decision(True, j, "приоритет спецтранспорту")
        if not others:
            return Decision(False, status="Других запросов нет: зелёный продлевается")
        for j in others:
            for g in self.L.stages[j].ped:
                n, w = obs.waiting.get(g), obs.max_wait.get(g) or 0.0
                if n is not None and n >= p.group_threshold:
                    return Decision(True, j, f"группа {n} чел. у перехода «{self.L.groups[g].title.lower()}»")
                if n and w >= p.max_wait_s:
                    return Decision(True, j, f"пешеходы ждут предельные {p.max_wait_s:.0f} с")
        served_known = all(obs.queue.get(g) is not None for g in st.veh)
        if served_known and all((obs.queue.get(g) or 0) == 0 and (obs.eta.get(g) is None or obs.eta.get(g) > p.gap_s)
                                for g in st.veh):
            return Decision(True, None, "разрыв в потоке: машины на подходах закончились")
        limit = p.veh_max_green_s if served_known else p.fixed_green_s
        if t >= limit:
            return Decision(True, None, f"максимальный зелёный {limit:.0f} с")
        return Decision(False, status=f"Продление: машины подходят ({t:.0f} из {limit:.0f} с)")

    def _event(self, level: str, kind: str, message: str, **data) -> None:
        self.events.append(ControllerEvent(level, kind, message, data))
