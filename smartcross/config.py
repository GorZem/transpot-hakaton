"""System configuration: pydantic models + YAML persistence."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

Point = tuple[float, float]  # normalized image coordinates, 0..1


class SiteConfig(BaseModel):
    name: str = "Пешеходный переход"
    crossing_length_m: float = Field(14.0, gt=0, description="Длина перехода, м")
    lanes_total: int = Field(4, ge=1, description="Полос движения суммарно в обоих направлениях")
    ped_speed_mps: float = Field(1.3, gt=0, description="Расчётная скорость пешехода, м/с")


class TimingConfig(BaseModel):
    """All durations in seconds."""
    veh_min_green_s: float = Field(12, ge=5, description="Минимальный зелёный для ТС")
    veh_min_green_empty_s: float = Field(4, ge=0, description="Минимальный зелёный ТС, если дорога пуста")
    veh_green_blink_s: float = Field(3, ge=0, description="Зелёный мигающий ТС")
    veh_yellow_s: float = Field(3, ge=3, description="Жёлтый ТС")
    all_red_s: float = Field(2, ge=1, description="Всё красное (очистка перехода от ТС)")
    ped_min_green_s: float = Field(7, ge=5, description="Минимальный зелёный пешеходам")
    ped_start_s: float = Field(4, ge=0, description="Время на реакцию/начало движения пешеходов")
    ped_blink_s: float = Field(3, ge=0, description="Зелёный мигающий пешеходам")
    ped_clearance_s: float = Field(2, ge=1, description="Всё красное после пешеходной фазы")
    ped_clear_extension_max_s: float = Field(8, ge=0, description="Макс. продление 'всё красное', если на переходе остались люди")
    ped_group_extra_per_person_s: float = Field(0.4, ge=0, description="Добавка к зелёному на человека сверх порога группы")
    ped_group_extra_max_s: float = Field(8, ge=0)
    min_ped_delay_empty_s: float = Field(2, ge=0, description="Задержка включения пешеходной фазы при отсутствии ТС")
    max_ped_wait_s: float = Field(50, ge=10, description="Максимальное ожидание пешехода (предел комфорта/безопасности)")
    gap_out_s: float = Field(3, ge=0.5, description="Разрыв в потоке ТС, после которого можно переключить досрочно")
    gap_out_factor: float = Field(0.5, ge=0, le=1, description="В разрыве потока порог накопленного ожидания умножается на это число")
    demand_debounce_s: float = Field(1.0, ge=0, description="Сколько секунд пешеход должен стоять в зоне ожидания, чтобы считаться вызовом")


class ControlConfig(BaseModel):
    group_threshold: int = Field(6, ge=2, description="Размер группы пешеходов для приоритета")
    group_delay_factor: float = Field(0.5, gt=0, le=1, description="Множитель порога накопленного ожидания для большой группы")
    saturation_flow_vphpl: float = Field(1800, gt=0, description="Поток насыщения, авт/ч на полосу")
    max_saturation: float = Field(0.9, gt=0.3, lt=1,
                                  description="Макс. степень насыщения полосы: зелёный ТС не короче, чем нужно для пропуска потока")
    veh_occupancy: float = Field(1.3, gt=0, description="Среднее число людей в ТС (для критерия суммарной задержки)")
    ped_time_weight: float = Field(3.0, gt=0, description="Вес времени пешехода относительно пассажира ТС")
    ped_impatience_s: float = Field(20, ge=0, description="Масштаб роста 'цены' ожидания пешехода, с (0 — линейная)")
    cycle_min_s: float = Field(30, gt=0)
    cycle_max_s: float = Field(120, gt=0)
    emergency_hold_max_s: float = Field(30, ge=0, description="Макс. удержание зелёного ТС для спецтранспорта")
    degraded_recall_s: float = Field(60, gt=0, description="В деградированном режиме пешеходная фаза не реже, чем раз в N секунд")


class FlowWindowConfig(BaseModel):
    """Adaptive averaging window for flow estimation (see docs/algorithms.md)."""
    min_s: float = Field(60, gt=0)
    max_s: float = Field(900, gt=0)
    target_count: int = Field(30, ge=5, description="Желаемое число ТС в окне (относит. ошибка ~1/sqrt(N))")
    ewma_alpha: float = Field(0.3, gt=0, le=1)


class FixedCycleConfig(BaseModel):
    veh_green_s: float = Field(40, ge=5)


class FallbackConfig(BaseModel):
    camera_timeout_s: float = Field(3, gt=0, description="Нет кадров дольше — камера считается потерянной")
    frozen_s: float = Field(6, gt=0, description="Кадр не меняется дольше — камера 'замёрзла'")
    dark_threshold: float = Field(8, ge=0, description="Средняя яркость ниже — камера закрыта/ослеплена")
    recovery_s: float = Field(10, gt=0, description="Камера должна быть исправна N секунд до возврата")
    all_lost_mode: Literal["fixed", "flashing"] = "fixed"
    fixed: FixedCycleConfig = FixedCycleConfig()


class NightConfig(BaseModel):
    enabled: bool = False
    start: str = "23:00"
    end: str = "06:00"
    mode: Literal["adaptive", "flashing"] = "adaptive"


class ZoneConfig(BaseModel):
    id: str
    type: Literal["ped_wait", "crosswalk", "approach", "count_line"]
    side: str | None = Field(None, description="Для ped_wait: сторона A/B")
    direction: str | None = Field(None, description="Для approach/count_line: направление движения")
    length_m: float | None = Field(None, description="Для approach: длина зоны вдоль дороги, м (для плотности)")
    points: list[Point]

    @field_validator("points")
    @classmethod
    def _check_points(cls, v: list[Point]) -> list[Point]:
        for x, y in v:
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError("координаты зон должны быть нормализованы в диапазоне 0..1")
        return v


class CameraConfig(BaseModel):
    id: str
    name: str = ""
    source: str = Field(..., description="Путь к файлу, rtsp://..., или индекс USB-камеры")
    loop: bool = True
    fps: float = Field(6, gt=0, le=30, description="Частота обработки кадров")
    enabled: bool = True
    zones: list[ZoneConfig] = []


class DetectorConfig(BaseModel):
    model: str = "models/yolo11n.pt"
    imgsz: int = 640
    conf: float = 0.3
    device: str = "cpu"
    threads: int = Field(2, ge=1, description="Потоков torch на камеру")
    tracker: str = "bytetrack.yaml"
    emergency_classes: list[str] = Field(default_factory=list,
                                         description="Имена классов спецтехники, если используется своя модель")
    emergency_light_detection: bool = True


class OutputConfig(BaseModel):
    driver: Literal["mock", "gpio"] = "mock"
    gpio_chip: str = "/dev/gpiochip0"
    gpio_lines: dict[str, int] = Field(default_factory=lambda: {
        "veh_red": 17, "veh_yellow": 27, "veh_green": 22, "ped_red": 23, "ped_green": 24})
    button_line: int | None = None


class Config(BaseModel):
    site: SiteConfig = SiteConfig()
    timing: TimingConfig = TimingConfig()
    control: ControlConfig = ControlConfig()
    flow_window: FlowWindowConfig = FlowWindowConfig()
    fallback: FallbackConfig = FallbackConfig()
    night: NightConfig = NightConfig()
    detector: DetectorConfig = DetectorConfig()
    output: OutputConfig = OutputConfig()
    cameras: list[CameraConfig] = []
    database_url: str = "sqlite:///data/smartcross.db"


class ConfigStore:
    """Thread-safe holder of the current config with YAML persistence."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cfg = load_config(self.path)
        self._version = 0

    @property
    def cfg(self) -> Config:
        with self._lock:
            return self._cfg

    @property
    def version(self) -> int:
        return self._version

    def update(self, cfg: Config) -> None:
        with self._lock:
            self._cfg = cfg
            self._version += 1
            save_config(cfg, self.path)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        return Config()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)


def save_config(cfg: Config, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)
