#!/usr/bin/env bash
# Nova Home on a Linux server (Ubuntu / Debian VPS). Normally driven from the
# office computer by move_to_cloud.bat; by hand:
#
#   bash install_server.sh prepare            install Docker, download the project, build the image
#   bash install_server.sh start [domain]     import /root/nova-migration.zip (if present) and start
#   bash install_server.sh update             new version from GitHub, rebuild, restart
#   bash install_server.sh flag               the same, started by the «/update» command of the bot
#   bash install_server.sh status | logs | backup
#
# Data (database, chats media, sessions, .env, logs) lives in /opt/novahome-data
# and is never touched by an update.
set -uo pipefail

MODE="${1:-update}"
DOMAIN_ARG="${2:-}"
BRANCH="${NOVA_BRANCH:-claude/new-session-85xp4a}"
REPO="${NOVA_REPO:-https://github.com/timurisnotbad/novahomedashboardminiapp.git}"
DIR="${NOVA_DIR:-/opt/novahome}"
DATA="${NOVA_DATA:-/opt/novahome-data}"
ZIP="${NOVA_ZIP:-/root/nova-migration.zip}"
export NOVA_DATA="$DATA"

say() { echo; echo "=== $* ==="; }
die() { echo; echo "!!! $*"; exit 1; }
[ "$(id -u)" -eq 0 ] || die "Запустите от root (sudo)"

dc() { ( cd "$DIR/deploy" && docker compose --env-file .env -f docker-compose.yml "$@" ); }

need_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    say "Устанавливаю Docker"
    curl -fsSL https://get.docker.com | sh || die "Docker не установился"
  fi
  docker compose version >/dev/null 2>&1 || die "Нет docker compose — обновите Docker"
}

fetch_code() {
  say "Код проекта → $DIR (ветка $BRANCH)"
  command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git >/dev/null; }
  if [ -d "$DIR/.git" ]; then
    git -C "$DIR" fetch --depth 1 origin "$BRANCH" && git -C "$DIR" reset --hard FETCH_HEAD || die "Не удалось скачать обновление"
  else
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$DIR" || die "Не удалось скачать проект"
  fi
  [ -f "$DIR/deploy/.env" ] || : > "$DIR/deploy/.env"
}

setenv() {  # setenv KEY VALUE  → deploy/.env (compose settings, not the app's .env)
  local f="$DIR/deploy/.env"
  grep -v "^$1=" "$f" > "$f.tmp" 2>/dev/null || true
  echo "$1=$2" >> "$f.tmp" && mv "$f.tmp" "$f"
}

version_now() { sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$DIR/backend/config.py" | head -1; }

import_zip() {
  [ -f "$ZIP" ] || return 0
  say "Переношу данные с офисного компьютера ($ZIP)"
  command -v python3 >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3 >/dev/null; }
  local tmp; tmp=$(mktemp -d)
  python3 -m zipfile -e "$ZIP" "$tmp" || die "Архив не распаковался"
  dc stop nova >/dev/null 2>&1 || true
  mkdir -p "$DATA"
  local stamp; stamp=$(date +%Y%m%d-%H%M%S)
  if [ -f "$DATA/nova_dashboard.db" ]; then
    mkdir -p "$DATA/backups" && cp "$DATA/nova_dashboard.db" "$DATA/backups/before-import-$stamp.db"
    rm -f "$DATA/nova_dashboard.db-wal" "$DATA/nova_dashboard.db-shm"
  fi
  [ -f "$tmp/env" ] && { [ -f "$DATA/.env" ] && cp "$DATA/.env" "$DATA/.env.before-$stamp"; cp "$tmp/env" "$DATA/.env"; }
  [ -f "$tmp/nova_dashboard.db" ] && cp "$tmp/nova_dashboard.db" "$DATA/nova_dashboard.db"
  [ -f "$tmp/rc_cookies.json" ] && cp "$tmp/rc_cookies.json" "$DATA/rc_cookies.json"
  [ -d "$tmp/data" ] && cp -a "$tmp/data/." "$DATA/"
  [ -d "$tmp/wa-auth" ] && { mkdir -p "$DATA/wa-auth" && cp -a "$tmp/wa-auth/." "$DATA/wa-auth/"; }
  [ -d "$tmp/cloudflared" ] && { mkdir -p "$DATA/cloudflared" && cp -a "$tmp/cloudflared/." "$DATA/cloudflared/"; }
  [ -f "$tmp/manifest.json" ] && cp "$tmp/manifest.json" "$DATA/migration-manifest.json"
  chmod 600 "$DATA/.env" 2>/dev/null || true
  rm -rf "$tmp"
  mv "$ZIP" "${ZIP%.zip}-imported-$stamp.zip"
  echo "    данные на месте: $DATA"
}

pick_access() {
  # how the world reaches the server: the old Cloudflare Tunnel, or own HTTPS
  local profile=""
  if [ -s "$DATA/cloudflared/token.txt" ]; then
    profile="tunnel-token"; setenv TUNNEL_TOKEN "$(tr -d '\r\n ' < "$DATA/cloudflared/token.txt")"
  elif [ -s "$DATA/cloudflared/config.yml" ]; then
    profile="tunnel-file"
  fi
  local dom="$DOMAIN_ARG"
  [ -z "$dom" ] && dom=$(sed -n 's/^CRM_DOMAIN=//p' "$DIR/deploy/.env" | head -1)
  if [ -z "$profile" ]; then
    [ -n "$dom" ] || die "Не найден туннель Cloudflare и не указан домен. Запустите: bash install_server.sh start crm.ваш-домен"
    profile="caddy"; setenv CRM_DOMAIN "$dom"
  fi
  setenv COMPOSE_PROFILES "$profile"
  if [ -n "$(ls -A "$DATA/wa-auth" 2>/dev/null)" ]; then setenv WA_BRIDGE 1; else setenv WA_BRIDGE 0; fi
  echo "    доступ снаружи: $profile"
}

ensure_env() {
  mkdir -p "$DATA"
  if [ ! -f "$DATA/.env" ]; then
    cp "$DIR/.env.example" "$DATA/.env"
    [ -n "$DOMAIN_ARG" ] && sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=https://$DOMAIN_ARG|" "$DATA/.env"
    echo "    создан пустой $DATA/.env — впишите ключи: nano $DATA/.env"
  fi
}

wait_health() {
  for _ in $(seq 1 40); do
    if curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
      echo "    сервер отвечает: $(curl -s http://127.0.0.1:8000/api/health | head -c 200)"
      return 0
    fi
    sleep 3
  done
  return 1
}

install_updater() {
  # «/update» in the bot drops a flag file into the data folder; systemd sees it and runs the update
  command -v systemctl >/dev/null 2>&1 || return 0
  cat > /etc/systemd/system/nova-update.service <<EOF
[Unit]
Description=Nova Home update (requested from the bot)
[Service]
Type=oneshot
Environment=NOVA_DIR=$DIR NOVA_DATA=$DATA NOVA_BRANCH=$BRANCH
ExecStart=/bin/bash $DIR/deploy/install_server.sh flag
EOF
  cat > /etc/systemd/system/nova-update.path <<EOF
[Unit]
Description=Nova Home update request watcher
[Path]
PathExists=$DATA/update.request
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload >/dev/null 2>&1 && systemctl enable --now nova-update.path >/dev/null 2>&1 || true
}

do_update() {
  local before after
  before=$(version_now)
  fetch_code
  after=$(version_now)
  say "Сборка и перезапуск (версия $before → $after)"
  dc up -d --build || return 1
  wait_health || return 1
  docker image prune -f >/dev/null 2>&1 || true
  echo "$after" > "$DATA/update.version"
  return 0
}

case "$MODE" in
  prepare)
    need_docker
    fetch_code
    say "Сборка образа (первый раз 5–10 минут)"
    dc build nova || die "Образ не собрался — пришлите этот вывод"
    say "Сервер готов к переносу данных (версия $(version_now))"
    ;;
  start)
    need_docker
    [ -d "$DIR/.git" ] || fetch_code
    import_zip
    ensure_env
    pick_access
    say "Запуск"
    dc up -d --build || die "Не запустилось — пришлите этот вывод"
    install_updater
    if wait_health; then
      say "Готово: Nova Home работает на этом сервере (версия $(version_now))"
      echo "Логи:      bash $DIR/deploy/install_server.sh logs"
      echo "Обновить:  команда /update боту, или  bash $DIR/deploy/install_server.sh update"
    else
      dc logs --tail 60 nova
      die "Сервер не ответил за 2 минуты — вывод выше покажет причину"
    fi
    ;;
  update)
    need_docker
    if do_update; then say "Обновлено до версии $(version_now)"; else dc logs --tail 40 nova; die "Обновление не удалось"; fi
    ;;
  flag)
    # started by systemd when the bot wrote update.request; the result goes back through a file the server reads
    rm -f "$DATA/update.request"
    LOG="$DATA/logs/update.log"; mkdir -p "$DATA/logs"
    if do_update > "$LOG" 2>&1; then
      echo "ok $(version_now)" > "$DATA/update.result"
    else
      { echo "fail"; tail -n 12 "$LOG"; } > "$DATA/update.result"
    fi
    ;;
  status)
    dc ps; curl -s http://127.0.0.1:8000/api/health; echo
    ;;
  logs)
    dc logs --tail 200 -f nova
    ;;
  backup)
    mkdir -p "$DATA/backups"
    OUT="$DATA/backups/manual-$(date +%Y%m%d-%H%M%S).db"
    dc exec -T nova python -c "import sqlite3,sys; s=sqlite3.connect('/data/nova_dashboard.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close()" "/data/backups/$(basename "$OUT")" \
      && echo "Копия базы: $OUT"
    ;;
  *)
    die "Неизвестная команда: $MODE (prepare | start | update | status | logs | backup)"
    ;;
esac
