# Сервер: эмулятор и центр

GPU-сервер (Ryzen 9 5950X, 64 ГБ, RTX 4090, Ubuntu 24.04). Обе части работают на нём как службы systemd:

| Что | Папка на сервере | Ветка | Служба | Порт внутри | Снаружи (с паролем) |
|---|---|---|---|---|---|
| Центр и админка | `/opt/smartcross/center` | `new` | `smartcross-center` | 127.0.0.1:8000 | `http://<сервер>/` |
| Эмулятор | `/opt/smartcross/emulator` | `emulator` | `smartcross-emulator` | 127.0.0.1:8100 | `http://<сервер>:8080/` |

Центр читает камеры и управляет светофорами эмулятора внутри сервера (`127.0.0.1:8100`), наружу
открыт только nginx с паролем (`/etc/nginx/smartcross.htpasswd`, логин и пароль — в `/root/smartcross-credentials.txt`).
Эмулятор рендерит без монитора через EGL драйвера NVIDIA (`EMULATOR_HEADLESS=1`).

## Как выкатить изменения

Просто `git push` в ветку `emulator` или `new`. Таймер `smartcross-autodeploy` раз в 20 с проверяет GitHub и:
- забирает новый коммит (`git reset --hard origin/<ветка>` — не правьте код на сервере руками, правки сотрутся);
- ставит зависимости, если изменился `requirements*.txt`;
- пересобирает админку, если изменилось что-то в `admin/`;
- перезапускает только изменившуюся часть. Не перезапускает, если изменились только веб-интерфейс эмулятора
  (`emulator/server/static/`), админка, документация или `deploy/` — такие правки видны после обновления страницы.

Эмулятор после перезапуска поднимается около минуты (прогрев модели движения); центр переподключается сам.
Локальные настройки центра — `config/local.yaml` на сервере (не в git, выкат его не трогает).

## Команды на сервере

```bash
journalctl -u smartcross-autodeploy -f          # что выкатывалось
journalctl -u smartcross-emulator -f            # журнал эмулятора
journalctl -u smartcross-center -f              # журнал центра
systemctl restart smartcross-emulator           # перезапуск вручную
systemctl start smartcross-autodeploy           # проверить GitHub прямо сейчас
nvidia-smi                                      # загрузка видеокарты
```

## Установка с нуля

Драйвер NVIDIA (`nvidia-driver-580`), `git nginx apache2-utils python3-venv build-essential ffmpeg`, Node.js 22 в `/opt/node`.
Клонировать ветки в `/opt/smartcross/{emulator,center}`, в каждой создать `.venv` и поставить зависимости
(центру — PyTorch с CUDA: `--index-url https://download.pytorch.org/whl/cu128`), собрать админку (`npm ci && npm run build`),
создать пароль `htpasswd -c /etc/nginx/smartcross.htpasswd smartcross` и выполнить `bash deploy/server/install-units.sh`.
