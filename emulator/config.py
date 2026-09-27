"""Настройки эмулятора. Идентификаторы объектов совпадают с data/sites.json системы."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

# Улицы, по которым моделируется движение (главные улицы участка).
STREETS = {
    "Краснодонская улица", "Краснодарская улица", "Ставропольская улица",
    "Новороссийская улица", "Совхозная улица",
}
# Границы участка (юг, запад, север, восток). Дороги за границей обрезаются, там появляются и исчезают машины.
BBOX = (55.6700, 37.7466, 55.6828, 37.7630)


@dataclass(frozen=True)
class ObjectSeed:
    id: str
    kind: str  # crossing — переход вне перекрёстка, cross — крестовой, tee — Т-образный
    title: str
    lat: float
    lon: float


OBJECTS = [
    ObjectSeed("p-krasnodonskaya-south", "crossing", "Переход на Краснодонской у ул. Судакова", 55.67288, 37.74887),
    ObjectSeed("p-krasnodonskaya-mid", "crossing", "Переход на Краснодонской, середина квартала", 55.67436, 37.74897),
    ObjectSeed("p-krasnodonskaya-north", "crossing", "Переход на Краснодонской у Краснодарской", 55.67541, 37.74904),
    ObjectSeed("p-krasnodarskaya", "crossing", "Переход на Краснодарской у Таганрогской", 55.67782, 37.75380),
    ObjectSeed("p-stavropolskaya", "crossing", "Переход на Ставропольской у Таганрогской", 55.68125, 37.75514),
    ObjectSeed("x-krasnodarskaya-krasnodonskaya", "cross", "Краснодарская × Краснодонская", 55.67751, 37.74918),
    ObjectSeed("x-krasnodonskaya-stavropolskaya", "cross", "Краснодонская × Ставропольская", 55.68089, 37.74939),
    ObjectSeed("x-krasnodarskaya-novorossiyskaya", "cross", "Краснодарская × Новороссийская", 55.67719, 37.75931),
    ObjectSeed("x-krasnodonskaya-sovkhoznaya", "cross", "Краснодонская × Совхозная", 55.67113, 37.74873),
    ObjectSeed("t-novorossiyskaya-stavropolskaya", "tee", "Новороссийская × Ставропольская", 55.68164, 37.76105),
]


@dataclass
class CameraSettings:
    width: int = 960           # размер выходного кадра
    height: int = 540
    fps: float = 10.0
    jpeg_quality: int = 80
    mount_height_m: float = 6.0
    # Модель широкоугольного объектива (OpenCV): fx = fy, k1, k2. Горизонтальный угол около 125°.
    focal_px: float = 360.0
    k1: float = -0.12
    k2: float = 0.01
    idle_stop_s: float = 5.0   # камеру без зрителей перестаём рендерить через столько секунд


@dataclass
class TrafficSettings:
    vehicles_per_hour: dict = field(default_factory=lambda: {"secondary": 520, "tertiary": 300})  # на направление
    pedestrians_per_min: float = 2.0      # на переход (у оборудованных объектов)
    other_pedestrians_per_min: float = 0.5  # на переходы обычных перекрёстков
    traffic_scale: float = 1.0
    pedestrian_scale: float = 1.0


@dataclass
class ControlSettings:
    watchdog_s: float = 20.0  # без команд и heartbeat дольше — объект возвращается к локальной программе


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8100
    sim_dt: float = 0.05
    render_hz: float = 30.0
    camera: CameraSettings = field(default_factory=CameraSettings)
    traffic: TrafficSettings = field(default_factory=TrafficSettings)
    control: ControlSettings = field(default_factory=ControlSettings)
    seed: int = 7
