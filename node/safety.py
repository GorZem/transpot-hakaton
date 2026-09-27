"""Модуль безопасности (аналог конфликт-монитора дорожного контроллера).

Не зависит от логики решений: смотрит только на сигналы, которые уходят на светофор.
Если команда недопустима, объект переводится в жёлтый мигающий.
Проверяет:
- одновременное разрешение конфликтующих движений;
- порядок сигналов (например, нельзя из зелёного сразу в красный);
- минимальные длительности мигания, жёлтого, красного с жёлтым;
- промежуточный такт: зелёный включается, только если конфликтующие группы стоят не меньше «все красные».
"""
from __future__ import annotations

from node.model import PED_MOVING, VEH_MOVING, Layout, Ped, Veh
from node.params import Params

VEH_NEXT = {
    Veh.RED: {Veh.RED_YELLOW},
    Veh.RED_YELLOW: {Veh.GREEN},
    Veh.GREEN: {Veh.GREEN_BLINK},
    Veh.GREEN_BLINK: {Veh.YELLOW},
    Veh.YELLOW: {Veh.RED},
    Veh.FLASH: {Veh.RED},
}
PED_NEXT = {
    Ped.RED: {Ped.GREEN},
    Ped.GREEN: {Ped.GREEN_BLINK},
    Ped.GREEN_BLINK: {Ped.RED},
    Ped.OFF: {Ped.RED},
}


NAMES = {Veh.RED: "красный", Veh.RED_YELLOW: "красный с жёлтым", Veh.GREEN: "зелёный",
         Veh.GREEN_BLINK: "зелёный мигающий", Veh.YELLOW: "жёлтый", Veh.FLASH: "жёлтый мигающий",
         Ped.RED: "красный", Ped.GREEN: "зелёный", Ped.GREEN_BLINK: "зелёный мигающий", Ped.OFF: "выключен"}


def _n(s) -> str:
    return NAMES.get(s, str(s))


class Safety:
    def __init__(self, layout: Layout, params: Params):
        self.L, self.p = layout, params
        self.prev: dict[str, str] = {}
        self.since: dict[str, float] = {}
        self.stopped_at: dict[str, float] = {}  # когда группа перестала двигаться

    def _moving(self, g: str, s: str) -> bool:
        return s in (VEH_MOVING if self.L.groups[g].kind == "veh" else PED_MOVING)

    def _min_dur(self, s: str) -> float:
        p = self.p
        return {Veh.GREEN_BLINK: p.green_blink_s, Veh.YELLOW: p.yellow_s, Veh.RED_YELLOW: p.red_yellow_s,
                Ped.GREEN_BLINK: p.ped_blink_s}.get(s, 0.0)

    def check(self, now: float, sig: dict[str, str]) -> list[str]:
        errs: list[str] = []
        for pair in self.L.conflicts:
            a, b = tuple(pair)
            if self._moving(a, sig[a]) and self._moving(b, sig[b]):
                errs.append(f"конфликт: «{self.L.groups[a].title}» и «{self.L.groups[b].title}» одновременно разрешены")
        for g, s in sig.items():
            prev = self.prev.get(g)
            if prev is None or s == prev:
                continue
            veh = self.L.groups[g].kind == "veh"
            to_safe = s in (Veh.FLASH, Ped.OFF)
            allowed = (VEH_NEXT if veh else PED_NEXT).get(prev, set())
            if not to_safe and s not in allowed:
                errs.append(f"«{self.L.groups[g].title}»: недопустимая смена {_n(prev)} → {_n(s)}")
            need = self._min_dur(prev)
            if not to_safe and now - self.since[g] + 1e-6 < need:
                errs.append(f"«{self.L.groups[g].title}»: {_n(prev)} длился {now - self.since[g]:.1f} с < {need:.1f} с")
            if s in (Veh.GREEN, Ped.GREEN):
                for c in self.L.conflicting(g):
                    if self._moving(c, sig[c]):
                        continue  # уже отмечено как конфликт
                    stop = self.stopped_at.get(c)
                    if stop is not None and now - stop + 1e-6 < self.p.all_red_s:
                        errs.append(f"«{self.L.groups[g].title}»: зелёный через {now - stop:.1f} с после "
                                    f"«{self.L.groups[c].title}», нужно ≥ {self.p.all_red_s:.0f} с")
        for g, s in sig.items():
            prev = self.prev.get(g)
            if prev != s:
                if prev is not None and self._moving(g, prev) and not self._moving(g, s):
                    self.stopped_at[g] = now
                self.since[g] = now
                self.prev[g] = s
        return errs
