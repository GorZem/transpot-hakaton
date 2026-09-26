"""Signal output drivers: mock (demo) and GPIO relays (embedded Linux PC / SBC).

The controller outputs logical signals; blinking is done here (1 Hz, 50% duty),
so the logic doesn't care about lamp timing.
"""
from __future__ import annotations

import logging
import time

from smartcross.config import OutputConfig
from smartcross.controller.fsm import PedSignal, VehSignal

log = logging.getLogger(__name__)


def lamps(veh: VehSignal, ped: PedSignal, t: float) -> dict[str, bool]:
    blink_on = (t % 1.0) < 0.5
    return {
        "veh_red": veh == VehSignal.RED,
        "veh_yellow": veh == VehSignal.YELLOW or (veh == VehSignal.YELLOW_BLINK and blink_on),
        "veh_green": veh == VehSignal.GREEN or (veh == VehSignal.GREEN_BLINK and blink_on),
        "ped_red": ped == PedSignal.RED,
        "ped_green": ped == PedSignal.GREEN or (ped == PedSignal.GREEN_BLINK and blink_on),
    }


class SignalOutput:
    def set(self, veh: VehSignal, ped: PedSignal) -> None:
        raise NotImplementedError

    def read_button(self) -> bool:
        return False

    def close(self) -> None:
        pass


class MockOutput(SignalOutput):
    def __init__(self) -> None:
        self.state: dict[str, bool] = {}
        self.button = False  # can be "pressed" from the UI

    def set(self, veh: VehSignal, ped: PedSignal) -> None:
        self.state = lamps(veh, ped, time.monotonic())

    def read_button(self) -> bool:
        b, self.button = self.button, False
        return b


class GpioOutput(SignalOutput):
    """Relays on GPIO lines via libgpiod v2 (python3-gpiod)."""

    def __init__(self, cfg: OutputConfig):
        import gpiod  # optional dependency, only on the target device
        from gpiod.line import Direction, Value

        self._Value = Value
        self.names = list(cfg.gpio_lines)
        config = {cfg.gpio_lines[n]: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)
                  for n in self.names}
        if cfg.button_line is not None:
            config[cfg.button_line] = gpiod.LineSettings(direction=Direction.INPUT)
        self.req = gpiod.request_lines(cfg.gpio_chip, consumer="smartcross", config=config)
        self.cfg = cfg

    def set(self, veh: VehSignal, ped: PedSignal) -> None:
        values = lamps(veh, ped, time.monotonic())
        self.req.set_values({self.cfg.gpio_lines[n]: self._Value.ACTIVE if values[n] else self._Value.INACTIVE
                             for n in self.names})

    def read_button(self) -> bool:
        if self.cfg.button_line is None:
            return False
        return self.req.get_value(self.cfg.button_line) == self._Value.ACTIVE

    def close(self) -> None:
        self.req.release()


def make_output(cfg: OutputConfig) -> SignalOutput:
    if cfg.driver == "gpio":
        try:
            return GpioOutput(cfg)
        except Exception:  # noqa: BLE001
            log.exception("GPIO unavailable, falling back to mock output")
    return MockOutput()
