# Развёртывание на встраиваемом ПК (Linux)

## Требования

- x86-64 или ARM64, 4+ ядра, 4 ГБ ОЗУ; GPU не нужен.
  Замер на ноутбуке: YOLO11n 640 px на CPU (PyTorch, 2 потока) = 40–50 мс/кадр.
  Оценка нагрузки: 2 камеры × 6 к/с × ~45 мс ≈ 0,55 с инференса в секунду, то есть 1–1,5 ядра.
- Linux (Debian/Ubuntu), Python 3.10+, ffmpeg.
- Сеть до IP-камер (RTSP), реле/вход дорожного контроллера (GPIO через libgpiod) или
  интеграция с контроллером.

## Вариант 1: Docker

```bash
docker compose up -d --build
# интерфейс: http://<ip>:8000
```

- Рабочий конфиг — `deploy/data/config.yaml`. При первом запуске он создаётся копией
  `config/default.yaml`. Статистика пишется в `data/smartcross.db` (SQLite).
- Веса модели скачиваются при сборке образа, в эксплуатации интернет не нужен.
- GPIO: раскомментировать `devices` в `docker-compose.yml`, в конфиге указать `output.driver: gpio`.

## Вариант 2: systemd

```bash
sudo useradd -r -m -d /opt/smartcross smartcross
sudo -u smartcross git clone https://github.com/GorZem/transpot-hakaton /opt/smartcross
cd /opt/smartcross
sudo -u smartcross python3 -m venv .venv
sudo -u smartcross .venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
sudo -u smartcross .venv/bin/pip install -r requirements.txt
sudo cp deploy/smartcross.service /etc/systemd/system/
sudo systemctl enable --now smartcross
```

`Restart=always`: при падении процесса systemd перезапускает его через 3 с. Пока процесс
перезапускается, светофором управляет штатный дорожный контроллер: SmartCross подключается к
нему как «детектор + вызов фазы» и не заменяет контроллер как устройство безопасности.

## Настройка

1. «Настройка → Камеры и зоны»: указать источник каждой камеры (`rtsp://user:pass@ip/stream`)
   и разметить зоны на кадре.
2. «Объект»: длина перехода, число полос, расчётная скорость пешехода (1,3 м/с; 1,0 м/с у
   больниц и школ).
3. «Аварийные режимы»: что делать при полном отказе камер (фиксированный цикл или жёлтый
   мигающий).
4. «Алгоритм управления → Вес времени пешехода»: баланс «водители ↔ пешеходы»
   (см. [algorithms.md](algorithms.md), кривая Парето).
5. Защитить изменения паролем: переменная окружения `SMARTCROSS_ADMIN_PASSWORD`
   (пользователь `admin`). Без неё интерфейс открыт всем в сети шкафа.

## Ускорение инференса

- `detector.imgsz`: 640 — быстро, 960 — дальше видит пешеходов (см. `camera_placement.md`).
- `detector.threads`: потоков torch на камеру.
- Экспорт в ONNX/OpenVINO (`yolo export model=models/yolo11n.pt format=openvino`) и указание
  пути в `detector.model` поддерживается Ultralytics без изменений кода. На нашей тестовой
  машине ONNX Runtime оказался медленнее PyTorch (95 мс против 45 мс), поэтому выбирать формат
  нужно замером на конкретном железе.

## Хранение статистики

- SQLite по умолчанию (WAL-режим): поминутные агрегаты трафика, журнал фаз, обслуживание
  пешеходов, события. ~0,5 МБ в сутки.
- PostgreSQL: `database_url: postgresql+psycopg://user:pass@host/smartcross`
  (установить `psycopg[binary]`).
- Экспорт в CSV: «Статистика → Экспорт CSV» или `GET /api/stats/export.csv?hours=24`.

## API

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/state` | текущее состояние: фаза, режим, наблюдения, камеры |
| GET | `/api/cameras/{id}/stream.mjpg` | видео с разметкой |
| GET | `/api/cameras/{id}/snapshot.jpg?raw=1` | кадр без разметки (для редактора зон) |
| GET/PUT | `/api/config` | конфигурация (валидируется, применяется на лету) |
| GET | `/api/config/schema` | JSON Schema конфигурации (по ней строится форма) |
| POST | `/api/cameras/{id}/fault` | имитация отказа `{"fault": "disconnect"/"freeze"/"dark"/null}` |
| POST | `/api/mode` | принудительный режим `{"mode": "fixed"/"flashing"/"adaptive"/null}` |
| POST | `/api/button` | виртуальная кнопка вызова |
| POST | `/api/conflict/reset` | сброс конфликт-монитора |
| GET | `/api/stats/summary`, `/timeseries`, `/classes`, `/waits`, `/export.csv` | статистика |
| GET | `/api/events`, `/api/phases` | журналы |

Полная документация OpenAPI: `http://<ip>:8000/docs`.
