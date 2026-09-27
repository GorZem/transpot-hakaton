"""Модель светофорного объекта: группы сигналов, фазы (стадии), конфликты, камеры, наблюдение.

Одна и та же модель описывает все три типа объектов:
- crossing — регулируемый пешеходный переход вне перекрёстка;
- tee      — Т-образный перекрёсток;
- cross    — крестовой перекрёсток.
"""
from __future__ import annotations

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
    covers: tuple[str, ...]  # id групп, чьи зоны камера видит (зоны ожидания, подходы)


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


def _axis(bearing: float) -> tuple[str, str, str, str]:
    """Названия подходов по азимуту главной дороги."""
    if bearing < 45 or bearing >= 135:
        return "n", "s", "с севера", "с юга"
    return "w", "e", "с запада", "с востока"


def build_layout(kind: str, bearing: float = 0.0, road_width_m: float = 14.0) -> Layout:
    a, b, ta, tb = _axis(bearing)
    if kind == "crossing":
        groups = [
            Group(a, "veh", f"Подход {ta}"),
            Group(b, "veh", f"Подход {tb}"),
            Group("cw", "ped", "Переход", road_width_m),
        ]
        stages = [Stage("S1", "Транспорт", veh=(a, b)), Stage("S2", "Пешеходы", ped=("cw",))]
        conflicts = {frozenset((a, "cw")), frozenset((b, "cw"))}
        cameras = [
            Camera("cam1", f"Камера 1: подход {ta}, переход", (a, "cw")),
            Camera("cam2", f"Камера 2: подход {tb}, переход", (b, "cw")),
        ]
    elif kind == "tee":
        groups = [
            Group(a, "veh", f"Главная {ta}"),
            Group(b, "veh", f"Главная {tb}"),
            Group("st", "veh", "Примыкание"),
            Group("cw_main", "ped", "Переход через главную", road_width_m),
            Group("cw_stem", "ped", "Переход через примыкание", 10.5),
        ]
        stages = [
            Stage("S1", "Главная дорога", veh=(a, b), ped=("cw_stem",)),
            Stage("S2", "Примыкание", veh=("st",)),
            Stage("S3", "Пешеходы через главную", ped=("cw_main",)),
        ]
        conflicts = {frozenset(p) for p in [(a, "st"), (b, "st"), (a, "cw_main"), (b, "cw_main"),
                                            ("st", "cw_stem"), ("st", "cw_main")]}
        cameras = [
            Camera("cam1", "Камера 1: напротив примыкания", ("st", "cw_stem", "cw_main")),
            Camera("cam2", "Камера 2: угол примыкания", (a, b, "cw_main")),
        ]
    elif kind == "cross":
        c, d, tc, td = ("w", "e", "с запада", "с востока") if a == "n" else ("n", "s", "с севера", "с юга")
        groups = [
            Group(a, "veh", f"Подход {ta}"), Group(b, "veh", f"Подход {tb}"),
            Group(c, "veh", f"Подход {tc}"), Group(d, "veh", f"Подход {td}"),
            Group(f"cw_{a}", "ped", f"Переход через северный рукав" if a == "n" else "Переход через западный рукав", road_width_m),
            Group(f"cw_{b}", "ped", f"Переход через южный рукав" if b == "s" else "Переход через восточный рукав", road_width_m),
            Group(f"cw_{c}", "ped", f"Переход через западный рукав" if c == "w" else "Переход через северный рукав", road_width_m),
            Group(f"cw_{d}", "ped", f"Переход через восточный рукав" if d == "e" else "Переход через южный рукав", road_width_m),
        ]
        # Пешеходы идут параллельно своему потоку машин: при движении по оси a-b
        # открыты переходы через рукава c и d.
        stages = [
            Stage("S1", "Ось " + ("север-юг" if a == "n" else "запад-восток"), veh=(a, b), ped=(f"cw_{c}", f"cw_{d}")),
            Stage("S2", "Ось " + ("запад-восток" if c == "w" else "север-юг"), veh=(c, d), ped=(f"cw_{a}", f"cw_{b}")),
        ]
        conflicts = {frozenset(p) for p in [(a, c), (a, d), (b, c), (b, d),
                                            (a, f"cw_{a}"), (a, f"cw_{b}"), (b, f"cw_{a}"), (b, f"cw_{b}"),
                                            (c, f"cw_{c}"), (c, f"cw_{d}"), (d, f"cw_{c}"), (d, f"cw_{d}")]}
        # Камеры по диагонали: каждая видит два перехода у дальнего угла и два подхода.
        cameras = [
            Camera("cam1", "Камера 1: диагональ", (b, d, f"cw_{b}", f"cw_{d}")),
            Camera("cam2", "Камера 2: диагональ", (a, c, f"cw_{a}", f"cw_{c}")),
        ]
    else:
        raise ValueError(f"неизвестный тип объекта: {kind}")
    return Layout(kind, {g.id: g for g in groups}, stages, conflicts, cameras)
