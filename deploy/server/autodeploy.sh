#!/usr/bin/env bash
# Автовыкат: раз в 20 с (systemd-таймер) проверяет ветки на GitHub и обновляет сервер.
#   emulator -> /opt/smartcross/emulator, служба smartcross-emulator
#   new      -> /opt/smartcross/center,   служба smartcross-center
# Перезапускается только изменившаяся часть. Правки только в веб-интерфейсе эмулятора
# (emulator/server/static) и в документации применяются без перезапуска; админка центра пересобирается.
set -u
exec 9>/run/smartcross-deploy.lock
flock -n 9 || exit 0

ROOT=/opt/smartcross
log() { echo "$(date '+%F %T') $*"; }

changed_only() {  # все изменённые файлы попадают под шаблон?
  local pattern=$1 files=$2
  [ -n "$files" ] && ! echo "$files" | grep -qvE "$pattern"
}

update() {
  local dir=$1 branch=$2 service=$3
  cd "$dir" || return
  git fetch -q origin "$branch" 2>/dev/null || { log "$service: GitHub недоступен"; return; }
  local old new files
  old=$(git rev-parse HEAD)
  new=$(git rev-parse "origin/$branch")
  [ "$old" = "$new" ] && return
  files=$(git diff --name-only "$old" "$new")
  git reset -q --hard "origin/$branch"
  log "$service: ${old:0:7} -> ${new:0:7} ($(git log -1 --format=%s))"

  if echo "$files" | grep -qE '(^|/)requirements[^/]*\.txt$'; then
    log "$service: зависимости изменились, ставлю"
    if [ -f emulator/requirements.txt ]; then .venv/bin/pip install -q -r emulator/requirements.txt; fi
    if [ -f requirements.txt ]; then .venv/bin/pip install -q -r requirements.txt; fi
  fi
  if [ -d admin ] && echo "$files" | grep -q '^admin/'; then
    log "$service: пересобираю админку"
    ( cd admin && export PATH=/opt/node/bin:$PATH &&
      { echo "$files" | grep -qE '^admin/package(-lock)?\.json$' && npm ci --no-audit --no-fund -s || true; } &&
      npm run build -s ) || log "$service: сборка админки упала"
  fi
  if echo "$files" | grep -q '^deploy/server/'; then
    log "$service: обновились файлы развёртывания — применяю"
    bash "$ROOT/emulator/deploy/server/install-units.sh"
  fi
  # без перезапуска: веб-интерфейс эмулятора, собранная админка, документация
  if changed_only '^(emulator/server/static/|admin/|deploy/|docs/)|\.md$' "$files"; then
    log "$service: перезапуск не нужен"
    return
  fi
  systemctl restart "$service"
  log "$service: перезапущен"
}

update "$ROOT/emulator" emulator smartcross-emulator
update "$ROOT/center" new smartcross-center
