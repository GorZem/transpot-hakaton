# Сервер: эмулятор и центр

GPU-сервер 141.105.65.164 (Ryzen 9 5950X, 64 ГБ, RTX 4090, Ubuntu 24.04). Обе части работают на нём как службы systemd:

| Что | Папка на сервере | Ветка | Служба | Порт внутри | Снаружи (по PIN) |
|---|---|---|---|---|---|
| Центр и админка | `/opt/smartcross/center` | `new` | `smartcross-center` | 127.0.0.1:8000 | `http://<сервер>/` |
| Эмулятор | `/opt/smartcross/emulator` | `emulator` | `smartcross-emulator` | 127.0.0.1:8100 | `http://<сервер>:8080/` |
| Вход по PIN | `deploy/server/gate.py` | `emulator` | `smartcross-gate` | 127.0.0.1:8090 | — |

Центр читает камеры и управляет светофорами эмулятора внутри сервера (`127.0.0.1:8100`), наружу
открыт только nginx. Вход по PIN-коду (`gate.py`, служба `smartcross-gate`): свой PIN у админки и у эмулятора,
после входа браузер помнит его 30 дней (выйти — `/__gate/logout`). PIN-коды хранятся только на сервере,
в `/etc/smartcross/gate.env` (`PIN_ADMIN`, `PIN_EMULATOR`, `GATE_SECRET`); после правки —
`systemctl restart smartcross-gate`. Сменить `GATE_SECRET` — выйти всем. Перебор PIN ограничен:
10 попыток в минуту с одного адреса.
Эмулятор рендерит без монитора через EGL драйвера NVIDIA (`EMULATOR_HEADLESS=1`).

Домены с HTTPS: https://transport.gorzem.com — админка, https://emulator.gorzem.com — эмулятор (PIN те же).
Они настроены отдельным файлом `/etc/nginx/sites-available/smartcross-domains` (не в репозитории, его не трогает
`install-units.sh`); он использует `map $connection_upgrade` и `limit_req_zone pinlogin` из `nginx-smartcross.conf` —
не переименовывайте их. Сертификат Let's Encrypt (`certbot`, имя `gorzem`) продлевается сам: `certbot.timer`.

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
создать `/etc/smartcross/gate.env` с PIN-кодами и выполнить `bash deploy/server/install-units.sh`.
