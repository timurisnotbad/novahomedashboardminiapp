#!/bin/sh
# Runs the three Nova processes in one container; if one dies, the container
# exits and Docker restarts it (restart: unless-stopped).
set -e
mkdir -p /data/inbox_media /data/wa-auth /data/tg /app/logs
# the WhatsApp QR session and the Telegram session live on the data volume
rm -rf /app/wa-bridge/auth && ln -s /data/wa-auth /app/wa-bridge/auth
rm -rf /app/data && ln -s /data /app/data

cd /app
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
SERVER=$!
if [ -n "$BOT_TOKEN" ]; then
  python bot.py &
  BOT=$!
fi
if [ "${WA_BRIDGE:-1}" = "1" ]; then
  (cd wa-bridge && node bridge.mjs) &
fi
wait $SERVER
