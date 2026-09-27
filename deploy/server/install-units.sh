#!/usr/bin/env bash
# Службы systemd и nginx из репозитория. Повторный запуск безопасен.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
install -m 644 "$HERE"/smartcross-emulator.service "$HERE"/smartcross-center.service "$HERE"/smartcross-gate.service \
  "$HERE"/smartcross-autodeploy.service "$HERE"/smartcross-autodeploy.timer /etc/systemd/system/
install -m 644 "$HERE"/nginx-smartcross.conf /etc/nginx/sites-available/smartcross
ln -sf /etc/nginx/sites-available/smartcross /etc/nginx/sites-enabled/smartcross
rm -f /etc/nginx/sites-enabled/default
systemctl daemon-reload
systemctl enable -q smartcross-emulator smartcross-center smartcross-gate smartcross-autodeploy.timer nginx
systemctl restart smartcross-gate   # вход по PIN: быстрый, перезапуск не мешает работе
nginx -t -q && systemctl reload nginx
