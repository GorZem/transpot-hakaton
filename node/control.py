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

# Константы прежней политики правил (policy="rules"), оставлена для сравнения на модели.
LEGACY_DELAY_MAX_S = 45.0
LEGACY_FLOW_SAT_VPH = 1500.0
LEGACY_GROUP_FACTOR = 0.35


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
    def __init__(self, layout: Layout, params: Params, now: float = 0.0, policy: str = "cost"):
        self.policy = policy  # cost — минимум задержки людей; rules — прежние правила (для сравнения)
        self._pass_until: float | None = None  # до какого момента пропускаем подъезжающую машину
        self.red_at: dict[str, float] = {g: now for g in layout.veh_groups()}
        self.queue_since: dict[str, float | None] = {}
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
        self._pass_until = None
        st = self.L.stages[stage]
        for g, x in self.L.groups.items():
            if x.kind == "veh":
                self.signals[g] = Veh.GREEN if g in st.veh else Veh.RED
                self.red_at[g] = now
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
        self._track_queues(now, obs)
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
            if self.signals[g] != Veh.RED:
                self.red_at[g] = now
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
        self._pass_until = None
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
        elif self.policy == "rules":
            d = (self._legacy_crossing if self.L.kind == "crossing" else self._legacy_intersection)(now, obs, t)
        else:
            d = self._decide_cost(now, obs, t)
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

    # ---------- решение по задержке людей ----------
    def _free_time(self, g: str, obs: Observation, R: float, typical: bool = False) -> float:
        """Сколько секунд к стоп-линии никто не подъедет. По камере — время до ближайшей машины;
        «обычно» — средний интервал между машинами при текущей интенсивности."""
        f = obs.flow_vph.get(g)
        if typical:
            return R if not f else min(R, 3600 / f)
        if obs.queue.get(g) is None:
            return 0.0
        eta = obs.eta.get(g)
        if eta is None:  # в зоне видимости (~6 с езды) машин нет: дальше — в среднем через интервал 3600/q
            return min(R, 6.0 + (3600 / f if f else R))
        return min(R, eta)

    def veh_cost(self, groups, R: float, obs: Observation, typical: bool = False) -> float:
        """Человеко-секунды, которые потеряют едущие на зелёный, если остановить их на R секунд:
        стоящие в очереди ждут всё время R, подъезжающие — в среднем половину оставшегося красного,
        плюс потери на торможение и разгон каждой остановленной машины."""
        p = self.p
        total = 0.0
        for g in groups:
            f = obs.flow_vph.get(g)
            q = (p.fallback_flow_vph if f is None else f) / 3600
            Q = obs.queue.get(g) or 0
            lead = p.green_blink_s + p.yellow_s  # столько ещё можно проехать после решения
            tail = max(0.0, R - max(0.0, self._free_time(g, obs, R + lead, typical) - lead))
            arrivals = q * tail
            total += p.veh_occupancy * (Q * R + q * tail * tail / 2 + p.stop_penalty_s * (Q + arrivals))
        return total

    def red_duration(self, j: int, obs: Observation) -> float:
        """Сколько простоит на красном транспорт текущей фазы, если включить фазу j."""
        p, st = self.p, self.L.stages[j]
        return (p.all_red_s + (p.red_yellow_s if st.veh else 0) + self.planned_green(j, obs)
                + (p.green_blink_s + p.yellow_s if st.veh else p.ped_blink_s) + p.all_red_s + p.red_yellow_s)

    def planned_green(self, j: int, obs: Observation) -> float:
        p, st = self.p, self.L.stages[j]
        if st.ped_only:  # мигание пешеходного добавляется в red_duration
            return max((self.ped_green_duration(g, obs) for g in st.ped), default=p.ped_min_green_s)
        peds = [self.ped_green_duration(g, obs) + p.ped_blink_s for g in st.ped if (obs.waiting.get(g) or 0) > 0]
        clear = max(((obs.queue.get(g) or 0) * p.headway_s for g in st.veh), default=0.0)
        return max([p.veh_min_green_s, clear] + peds)

    def wait_cost(self, j: int, obs: Observation, now: float) -> float:
        """Накопленное ожидание тех, кого обслужит фаза j, в человеко-секундах."""
        p, st = self.p, self.L.stages[j]
        w = 0.0
        for g in st.ped:
            n = obs.waiting.get(g)
            if n is None:  # зона не видна: как будто один человек ждёт с момента планового вызова
                w += p.ped_weight * max(0.0, now - self.last_served[g] - p.blind_call_s)
            else:
                ws = obs.wait_sum.get(g)
                w += p.ped_weight * (ws if ws is not None else n * (obs.max_wait.get(g) or 0) / 2)
        for g in st.veh:
            red = now - self.red_at.get(g, now)
            Q = obs.queue.get(g)
            if Q is None:  # подход не виден: очередь по оценке потока
                Q = round(p.fallback_flow_vph / 3600 * red)
                waited = red
            else:  # машины ждут с момента, когда встала первая из очереди
                since = self.queue_since.get(g)
                waited = now - max(self.red_at.get(g, now), now if since is None else since)
            w += p.veh_occupancy * Q * waited / 2
            eta = obs.eta.get(g)
            to_green = p.green_blink_s + p.yellow_s + p.all_red_s + p.red_yellow_s
            if eta is not None and to_green - 2 <= eta <= to_green + 8:
                w += p.veh_occupancy * (p.stop_penalty_s + p.veh_min_green_s / 2)
        return w

    def _track_queues(self, now: float, obs: Observation) -> None:
        for g in self.L.veh_groups():
            if (obs.queue.get(g) or 0) > 0:
                if self.queue_since.get(g) is None:
                    self.queue_since[g] = now
            else:
                self.queue_since[g] = None

    def _decide_cost(self, now: float, obs: Observation, t: float) -> Decision:
        p, st = self.p, self.L.stages[self.stage]
        q_all = self.flow_estimate(obs, st.veh)
        gmin = p.veh_min_green_empty_s if (q_all is not None and q_all < p.empty_road_vph) else p.veh_min_green_s
        cands = [j for j in range(len(self.L.stages)) if j != self.stage and self._stage_demand(j, obs, now)]
        rows = []
        for j in cands:
            R = self.red_duration(j, obs)
            C = self.veh_cost(st.veh, R, obs)
            # удобный момент: если сейчас переключиться дешевле обычного, порог снижается на величину экономии
            thr = max(0.0, 2 * C - self.veh_cost(st.veh, R, obs, typical=True))
            rows.append((j, self.wait_cost(j, obs, now), thr, R, C))
        row = max(rows, key=lambda r: r[1] - r[2], default=None)
        best = row[0] if row else None
        peds = [g for j in cands for g in self.L.stages[j].ped]
        n_wait = sum(obs.waiting.get(g) or 0 for g in peds)
        w_max = max((obs.max_wait.get(g) or 0 for g in peds), default=0.0)
        self.info = {"flow_vph": None if q_all is None else round(q_all), "min_green_s": gmin, "green_s": round(t),
                     "waiting": n_wait, "max_wait_s": round(w_max, 1),
                     "wait_cost": None if row is None else round(row[1]),
                     "switch_cost": None if row is None else round(row[4]),
                     "threshold": None if row is None else round(row[2]),
                     "red_s": None if row is None else round(row[3], 1),
                     "candidate": None if best is None else self.L.stages[best].title,
                     "group": n_wait >= p.group_threshold}
        if t < gmin - EPS:
            return Decision(False, status=f"Минимальный зелёный {gmin:.0f} с")
        if obs.emergency & set(st.veh):
            return Decision(False, status="Удержание: приближается спецтранспорт")
        for j in cands:
            if obs.emergency & set(self.L.stages[j].veh):
                return Decision(True, j, "приоритет спецтранспорту")
        if row is None:
            return Decision(False, status="Других запросов нет: зелёный остаётся")
        _, W, C, R, C_now = row
        nxt = self.L.stages[best]
        for g in nxt.ped:  # предельное ожидание — страховка поверх расчёта
            if (obs.waiting.get(g) or 0) and (obs.max_wait.get(g) or 0) >= p.max_wait_s - EPS:
                return Decision(True, best, f"пешеходы ждут предельные {p.max_wait_s:.0f} с")
        for g in nxt.ped:  # плановый вызов для невидимых пешеходов
            if obs.waiting.get(g) is None and now - self.last_served[g] >= p.blind_call_s:
                return Decision(True, best, f"пешеходы не видны: плановый вызов раз в {p.blind_call_s:.0f} с")
        if nxt.ped_only and w_max < p.delay_min_s:
            return Decision(False, status=f"Ждут {n_wait} чел., минимальная задержка {p.delay_min_s:.0f} с")
        if not nxt.ped_only and t >= p.veh_max_green_s - EPS:
            return Decision(True, best, f"максимальный зелёный {p.veh_max_green_s:.0f} с")
        if not nxt.ped_only and all(obs.queue.get(g) == 0 and (obs.eta.get(g) is None or obs.eta.get(g) > p.gap_s) for g in st.veh):
            # на перекрёстке пустой зелёный — потерянное время для другой улицы
            return Decision(True, best, "зелёным никто не пользуется: машин на подходах нет")
        if W >= C:
            # одиночную машину, которая вот-вот проедет, выгоднее пропустить, чем остановить
            # скорость роста ожидания: каждый ждущий пешеход и каждый человек в стоящей машине
            rate = p.ped_weight * n_wait + p.veh_occupancy * sum(obs.queue.get(g) or 0 for g in nxt.veh)
            for g in st.veh:
                eta, Q = obs.eta.get(g), obs.queue.get(g)
                if Q == 0 and eta is not None and eta <= p.lookahead_s and                         p.veh_occupancy * (R - eta + p.stop_penalty_s) > max(rate, p.ped_weight) * (eta + 1):
                    if self._pass_until is None:
                        self._pass_until = now + eta + 1
                    if now < self._pass_until:
                        return Decision(False, status=f"Пропускаем подъезжающую машину ({eta:.0f} с до стоп-линии)")
            gap = " (удобный момент: разрыв в потоке)" if C < C_now - 0.5 else ""
            return Decision(True, best, f"ожидание {W:.0f} чел·с ≥ порога {C:.0f} чел·с{gap}")
        who = f"ждут {n_wait} чел." if n_wait else "ждут машины"
        return Decision(False, status=f"{nxt.title}: {who}, накоплено {W:.0f} из {C:.0f} чел·с")

    # ---------- прежние правила (policy="rules"), для сравнения ----------
    def _legacy_crossing(self, now: float, obs: Observation, t: float) -> Decision:
        p, st = self.p, self.L.stages[self.stage]
        ped_stage = next(i for i, s in enumerate(self.L.stages) if s.ped_only)
        cw = self.L.stages[ped_stage].ped[0]
        q = self.flow_estimate(obs, st.veh) or 0.0
        blind = any(obs.flow_vph.get(g) is None for g in st.veh)
        gmin = p.veh_min_green_empty_s if q < p.empty_road_vph else p.veh_min_green_s
        D = p.delay_min_s + (LEGACY_DELAY_MAX_S - p.delay_min_s) * min(1.0, q / LEGACY_FLOW_SAT_VPH)
        n = obs.waiting.get(cw)
        wait = obs.max_wait.get(cw) or 0.0
        group = n is not None and n >= p.group_threshold
        d_eff = max(p.delay_min_s, D * LEGACY_GROUP_FACTOR) if group else D
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

    def _legacy_intersection(self, now: float, obs: Observation, t: float) -> Decision:
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
