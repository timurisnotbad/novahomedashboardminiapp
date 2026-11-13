#!/bin/sh
# Runs the Nova processes in one container.
#  * server (uvicorn) is the main process: if it dies the container exits and
#    Docker starts it again (restart: unless-stopped);
#  * the bot and the WhatsApp bridge run in restart loops — a crash of either
#    never takes the server down, and the server's watchdog kills a stuck bot
#    so that this loop starts a fresh one.
mkdir -p /data/inbox_media /data/wa-auth /data/tg /data/logs /data/backups

# Everything persistent lives on /data: settings, sessions, logs.
rm -rf /app/wa-bridge/auth && ln -s /data/wa-auth /app/wa-bridge/auth
rm -rf /app/data && ln -s /data /app/data
rm -rf /app/logs && ln -s /data/logs /app/logs
[ -f /data/.env ] && ln -sf /data/.env /app/.env
[ -f /data/rc_cookies.json ] && export RC_COOKIES_FILE=/data/rc_cookies.json

cd /app
loop() {  # loop <name> <pause after a config error> <command...>
  name=$1; shift
  while true; do
    "$@"; code=$?
    echo "[$name] exited with code $code at $(date '+%F %T')"
    if [ "$code" = "2" ]; then sleep 300; else sleep 15; fi   # 2 = settings problem: retrying fast is pointless
  done
}

python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
SERVER=$!
loop bot python bot.py &
if [ "${WA_BRIDGE:-0}" = "1" ]; then
  ( cd wa-bridge && loop whatsapp node bridge.mjs ) &
fi
wait $SERVER
