"""Модель светофорного объекта: группы сигналов, фазы (стадии), конфликты, камеры, наблюдение.

Одна и та же модель описывает все три типа объектов:
- crossing — регулируемый пешеходный переход вне перекрёстка;
- tee      — Т-образный перекрёсток;
- cross    — крестовой перекрёсток.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


class Veh(str, Enum):
    RED = "red"
    RED_YELLOW = "red_yellow"
    GREEN = "green"
    GREEN_BLINK = "green_blink"
    YELLOW = "yellow"
    FLASH = "yellow_flash"  # жёлтый мигающий: объект не регулируется


class Ped(str, Enum):
    RED = "red"
    GREEN = "green"
    GREEN_BLINK = "green_blink"
    OFF = "off"


VEH_MOVING = {Veh.GREEN, Veh.GREEN_BLINK, Veh.YELLOW}
PED_MOVING = {Ped.GREEN, Ped.GREEN_BLINK}


class Mode(str, Enum):
    ADAPTIVE = "adaptive"    # все камеры работают
    DEGRADED = "degraded"    # часть зон не видна
    FIXED = "fixed"          # резервный фиксированный план
    FLASHING = "flashing"    # жёлтый мигающий (сработала защита или оператор)


MODE_TITLES = {
    Mode.ADAPTIVE: "Адаптивный",
    Mode.DEGRADED: "Деградированный",
    Mode.FIXED: "Фиксированный план",
    Mode.FLASHING: "Жёлтый мигающий",
}


@dataclass(frozen=True)
class Group:
    id: str
    kind: str  # "veh" | "ped"
    title: str
    length_m: float = 0.0  # для пешеходных: длина перехода


@dataclass(frozen=True)
class Stage:
    id: str
    title: str
    veh: tuple[str, ...] = ()
    ped: tuple[str, ...] = ()

    @property
    def ped_only(self) -> bool:
        return not self.veh


@dataclass(frozen=True)
class Camera:
    id: str
    title: str
    covers: tuple[str, ...]  # группы, чьи зоны камера видит (считается по калибровке камеры)


@dataclass
class Layout:
    kind: str
    groups: dict[str, Group]
    stages: list[Stage]
    conflicts: set[frozenset[str]]
    cameras: list[Camera]

    def veh_groups(self) -> list[str]:
        return [g for g, x in self.groups.items() if x.kind == "veh"]

    def ped_groups(self) -> list[str]:
        return [g for g, x in self.groups.items() if x.kind == "ped"]

    def conflicting(self, gid: str) -> set[str]:
        return {next(iter(p - {gid})) for p in self.conflicts if gid in p}

    def stages_serving(self, gid: str) -> list[int]:
        return [i for i, s in enumerate(self.stages) if gid in s.veh or gid in s.ped]


@dataclass
class Observation:
    """Сводка двух камер, единственный вход контроллера. None — зона не видна."""
    waiting: dict[str, int | None] = field(default_factory=dict)       # пешеходная группа -> ждут
    max_wait: dict[str, float | None] = field(default_factory=dict)    # -> дольше всех ждёт, с
    on_crosswalk: dict[str, int | None] = field(default_factory=dict)  # -> идут по переходу
    queue: dict[str, int | None] = field(default_factory=dict)         # подход -> стоят у стоп-линии
    eta: dict[str, float | None] = field(default_factory=dict)         # -> ближайшая машина до стоп-линии, с
    flow_vph: dict[str, float | None] = field(default_factory=dict)    # -> интенсивность, авт/ч
    emergency: set[str] = field(default_factory=set)                   # подходы со спецтранспортом
    cameras: dict[str, bool] = field(default_factory=dict)             # камера -> исправна

    def total_waiting(self, groups: list[str]) -> int:
        return sum(self.waiting.get(g) or 0 for g in groups)


def _len(cw: dict) -> float:
    return math.hypot(cw["b"][0] - cw["a"][0], cw["b"][1] - cw["a"][1])


def build_layout(site: dict) -> Layout:
    """Модель объекта из паспорта. Группы сигналов — как у дорожного контроллера на объекте:
    переход — veh / ped; перекрёсток — veh_A, veh_B (транспорт по осям) и ped_A, ped_B
    (пешеходы поперёк улиц оси A и B). Пешеходы идут параллельно транспорту другой оси."""
    titles = {g["id"]: g["title"] for g in site.get("signal_groups", [])}
    lengths: dict[str, float] = {}
    for cw in site.get("crosswalks", []):
        lengths[cw["group"]] = max(lengths.get(cw["group"], 0.0), round(_len(cw), 1))
    cams = [Camera(c["id"], c["title"], ()) for c in site.get("cameras", [])]
    if site["kind"] == "crossing":
        groups = [Group("veh", "veh", titles.get("veh", "Транспорт")),
                  Group("ped", "ped", titles.get("ped", "Пешеходы через переход"), lengths.get("ped", 14.0))]
        stages = [Stage("S1", "Транспорт", veh=("veh",)), Stage("S2", "Пешеходы", ped=("ped",))]
        conflicts = {frozenset(("veh", "ped"))}
    else:
        axes = site.get("axes", {})
        groups = [
            Group("veh_A", "veh", titles.get("veh_A", f"Транспорт: {axes.get('A', 'ось A')}")),
            Group("veh_B", "veh", titles.get("veh_B", f"Транспорт: {axes.get('B', 'ось B')}")),
            Group("ped_A", "ped", titles.get("ped_A", f"Пешеходы поперёк: {axes.get('A', 'ось A')}"), lengths.get("ped_A", 14.0)),
            Group("ped_B", "ped", titles.get("ped_B", f"Пешеходы поперёк: {axes.get('B', 'ось B')}"), lengths.get("ped_B", 14.0)),
        ]
        a, b = axes.get("A", "ось A"), axes.get("B", "ось B")
        stages = [Stage("S1", f"Движение по {a}", veh=("veh_A",), ped=("ped_B",)),
                  Stage("S2", f"Движение по {b}", veh=("veh_B",), ped=("ped_A",))]
        conflicts = {frozenset(("veh_A", "veh_B")), frozenset(("veh_A", "ped_A")), frozenset(("veh_B", "ped_B"))}
    return Layout(site["kind"], {g.id: g for g in groups}, stages, conflicts, cams)
