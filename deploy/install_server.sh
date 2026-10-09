#!/usr/bin/env bash
# Nova Home — one-command install on a fresh Ubuntu/Debian VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/timurisnotbad/novahomedashboardminiapp/main/deploy/install_server.sh | sudo bash -s -- crm.example.com
#
# What it does: installs Docker, clones the project to /opt/novahome, creates
# .env (or keeps yours), builds the image, starts the app behind Caddy with a
# free HTTPS certificate for the domain you pass. Re-running = update.
set -euo pipefail

DOMAIN="${1:-}"
BRANCH="${NOVA_BRANCH:-main}"
REPO="${NOVA_REPO:-https://github.com/timurisnotbad/novahomedashboardminiapp.git}"
DIR=/opt/novahome

if [ -z "$DOMAIN" ]; then
  echo "Укажите домен: bash install_server.sh crm.ваш-домен.com"; exit 1
fi
if [ "$(id -u)" -ne 0 ]; then echo "Запустите через sudo"; exit 1; fi

echo "[1/5] Docker..."
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

echo "[2/5] Код проекта → $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch origin "$BRANCH" && git -C "$DIR" reset --hard "origin/$BRANCH"
else
  apt-get update -qq && apt-get install -y -qq git >/dev/null
  git clone --branch "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"

echo "[3/5] Настройки .env"
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=https://$DOMAIN|" .env
  echo "  создан .env — впишите BOT_TOKEN, RC_TOKEN, OWNER_IDS, WAZZUP_API_KEY: nano $DIR/.env"
else
  python3 setup_env.py || true
fi
echo "CRM_DOMAIN=$DOMAIN" > deploy/.env

echo "[4/5] Сборка и запуск (первый раз 3–5 минут)..."
cd deploy
docker compose --env-file .env -f docker-compose.yml up -d --build

echo "[5/5] Проверка..."
sleep 8
if docker compose --env-file .env -f docker-compose.yml exec -T nova curl -sf http://localhost:8000/api/health >/dev/null; then
  echo
  echo "Готово: https://$DOMAIN/crm/   (сертификат появится через ~1 минуту после того, как DNS укажет на этот сервер)"
  echo "Логи:   cd $DIR/deploy && docker compose logs -f nova"
  echo "Обновить: повторно запустить эту же команду"
else
  echo "Сервер не ответил — смотрите: cd $DIR/deploy && docker compose logs nova"
fi
