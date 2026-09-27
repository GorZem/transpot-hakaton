"""Светофорные контроллеры (имитация дорожного контроллера на объекте).

Режимы:
- local  — объект работает по своей фиксированной программе;
- remote — состояния групп задаёт внешняя система командами через API;
- flash  — жёлтый мигающий: сработал конфликт-монитор или оператор перевёл объект вручную.

В режиме remote система обязана присылать команды или heartbeat не реже чем раз в watchdog_s,
иначе объект возвращается в local (как настоящий контроллер при потере связи с центром).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

VEH_STATES = ("red", "red_yellow", "green", "green_blink", "yellow", "flash_yellow", "off")
PED_STATES = ("red", "green", "green_blink", "off")
VEH_MOVING = {"green", "green_blink", "yellow"}
PED_MOVING = {"green", "green_blink"}


@dataclass
class Group:
    name: str
    kind: str          # veh | ped
    label: str
    state: str = "red"
    since: float = 0.0


@dataclass
class SignalEvent:
    ts: float
    object_id: str
    level: str
    message: str


def _program_crossing() -> list[tuple[float, dict]]:
    return [
        (35.0, {"veh": "green", "ped": "red"}),
        (3.0, {"veh": "green_blink", "ped": "red"}),
        (3.0, {"veh": "yellow", "ped": "red"}),
        (2.0, {"veh": "red", "ped": "red"}),
        (12.0, {"veh": "red", "ped": "green"}),
        (3.0, {"veh": "red", "ped": "green_blink"}),
        (2.0, {"veh": "red", "ped": "red"}),
        (2.0, {"veh": "red_yellow", "ped": "red"}),
    ]


def _program_junction(ga: float = 30.0, gb: float = 22.0) -> list[tuple[float, dict]]:
    def st(va, vb, pa, pb):
        return {"veh_A": va, "veh_B": vb, "ped_A": pa, "ped_B": pb}
    # пешеходы ped_B идут через рукава оси B параллельно транспорту оси A, и наоборот
    return [
        (ga - 3, st("green", "red", "red", "green")),
        (3.0, st("green", "red", "red", "green_blink")),
        (3.0, st("green_blink", "red", "red", "red")),
        (3.0, st("yellow", "red", "red", "red")),
        (2.0, st("red", "red_yellow", "red", "red")),
        (gb - 3, st("red", "green", "green", "red")),
        (3.0, st("red", "green", "green_blink", "red")),
        (3.0, st("red", "green_blink", "red", "red")),
        (3.0, st("red", "yellow", "red", "red")),
        (2.0, st("red_yellow", "red", "red", "red")),
    ]


@dataclass
class SignalController:
    node_id: int
    object_id: str | None            # оборудованный объект пилота или обычный светофор района
    kind: str                        # crossing | junction
    groups: dict[str, Group] = field(default_factory=dict)
    conflicts: set = field(default_factory=set)
    program: list = field(default_factory=list)
    mode: str = "local"
    t_prog: float = 0.0
    step: int = 0
    last_contact: float = 0.0        # время последней команды (монотонное)
    watchdog_s: float = 20.0
    events: list = field(default_factory=list)
    commands: int = 0

    @classmethod
    def for_node(cls, node, watchdog_s: float, offset: float = 0.0) -> "SignalController":
        if node.kind == "crossing":
            street = node.axis_names.get("A", "")
            c = cls(node.id, node.object_id, "crossing", watchdog_s=watchdog_s)
            c.groups = {"veh": Group("veh", "veh", f"Транспорт: {street}"),
                        "ped": Group("ped", "ped", "Пешеходы через переход")}
            c.conflicts = {frozenset(("veh", "ped"))}
            c.program = _program_crossing()
        else:
            a, b = node.axis_names.get("A", "ось A"), node.axis_names.get("B", "ось B")
            c = cls(node.id, node.object_id, "junction", watchdog_s=watchdog_s)
            c.groups = {
                "veh_A": Group("veh_A", "veh", f"Транспорт: {a}"),
                "veh_B": Group("veh_B", "veh", f"Транспорт: {b}"),
                "ped_A": Group("ped_A", "ped", f"Пешеходы поперёк: {a}"),
                "ped_B": Group("ped_B", "ped", f"Пешеходы поперёк: {b}"),
            }
            c.conflicts = {frozenset(("veh_A", "veh_B")), frozenset(("veh_A", "ped_A")),
                           frozenset(("veh_B", "ped_B"))}
            c.program = _program_junction()
        c._advance(offset % sum(d for d, _ in c.program), 0.0)
        return c

    # ---------------------------------------------------------------- работа
    def tick(self, dt: float, sim_t: float) -> None:
        if self.mode == "remote" and time.monotonic() - self.last_contact > self.watchdog_s:
            self._log("warn", f"нет команд {self.watchdog_s:.0f} с: возврат к локальной программе")
            self.mode = "local"
            self.t_prog, self.step = 0.0, 0
            self._apply(self.program[0][1], sim_t)
        if self.mode == "local":
            self._advance(dt, sim_t)

    def _advance(self, dt: float, sim_t: float) -> None:
        self.t_prog += dt
        while self.t_prog >= self.program[self.step][0]:
            self.t_prog -= self.program[self.step][0]
            self.step = (self.step + 1) % len(self.program)
        self._apply(self.program[self.step][1], sim_t)

    def _apply(self, states: dict, sim_t: float) -> None:
        for name, st in states.items():
            g = self.groups[name]
            if g.state != st:
                g.state, g.since = st, sim_t

    # ---------------------------------------------------------------- команды
    def command(self, states: dict[str, str], sim_t: float) -> tuple[bool, str]:
        """Команда от системы. Возвращает (принято, сообщение)."""
        for name, st in states.items():
            if name not in self.groups:
                return False, f"нет группы {name}; есть: {', '.join(self.groups)}"
            allowed = VEH_STATES if self.groups[name].kind == "veh" else PED_STATES
            if st not in allowed:
                return False, f"{name}: недопустимое состояние {st}; допустимы: {', '.join(allowed)}"
        if self.mode == "flash":
            return False, "объект в жёлтом мигающем после конфликта, нужен сброс (reset)"
        merged = {n: g.state for n, g in self.groups.items()} | states
        for pair in self.conflicts:
            a, b = tuple(pair)
            if self._moving(a, merged[a]) and self._moving(b, merged[b]):
                self._log("error", f"конфликт-монитор: {a}={merged[a]} и {b}={merged[b]} одновременно. Жёлтый мигающий")
                self.set_flash(sim_t)
                return False, "конфликт сигналов: объект переведён в жёлтый мигающий"
        warn = self._check_sequence(states)
        if self.mode != "remote":
            self._log("info", "управление передано внешней системе")
        self.mode = "remote"
        self.last_contact = time.monotonic()
        self.commands += 1
        self._apply(states, sim_t)
        return True, warn or "ok"

    def heartbeat(self) -> None:
        if self.mode == "remote":
            self.last_contact = time.monotonic()

    def release(self, sim_t: float) -> None:
        self.mode = "local"
        self.t_prog, self.step = 0.0, 0
        self._apply(self.program[0][1], sim_t)
        self._log("info", "возврат к локальной программе")

    def set_flash(self, sim_t: float) -> None:
        self.mode = "flash"
        for g in self.groups.values():
            g.state, g.since = ("flash_yellow" if g.kind == "veh" else "off"), sim_t

    def _moving(self, name: str, state: str) -> bool:
        return state in (VEH_MOVING if self.groups[name].kind == "veh" else PED_MOVING)

    def _check_sequence(self, states: dict) -> str:
        """Предупреждения о нарушении порядка сигналов (объект их исполняет, но пишет в журнал)."""
        bad = []
        ok_next = {"green": {"green_blink", "yellow"}, "green_blink": {"yellow"}, "yellow": {"red"},
                   "red": {"red_yellow", "green"}, "red_yellow": {"green"}}
        for name, st in states.items():
            g = self.groups[name]
            if g.kind == "veh" and st != g.state and g.state in ok_next and st not in ok_next[g.state] | {"flash_yellow", "off"}:
                bad.append(f"{name}: {g.state} → {st}")
        if bad:
            msg = "нарушен порядок сигналов: " + "; ".join(bad)
            self._log("warn", msg)
            return msg
        return ""

    def _log(self, level: str, message: str) -> None:
        self.events.append(SignalEvent(time.time(), self.object_id or f"node-{self.node_id}", level, message))
        del self.events[:-200]

    # ---------------------------------------------------------------- для машин и пешеходов
    def veh_state(self, axis: str) -> str:
        return self.groups["veh" if self.kind == "crossing" else f"veh_{axis}"].state

    def ped_state(self, group: str) -> str:
        return self.groups[group].state

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "groups": {n: {"kind": g.kind, "label": g.label, "state": g.state} for n, g in self.groups.items()},
            "commands": self.commands,
        }
