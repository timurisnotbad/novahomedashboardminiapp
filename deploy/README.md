# Nova Home на облачном сервере (Linux VPS)

Короткая инструкция для владельца — в корне проекта: `ПЕРЕЕЗД-В-ОБЛАКО.txt`
(купить сервер → запустить `move_to_cloud.bat`). Здесь — как это устроено.

## Состав

| Файл | Что делает |
|---|---|
| `move_to_cloud.bat` → `deploy/move_to_cloud.py` | Запускается на офисном компьютере: SSH-ключ, подготовка сервера, остановка Nova здесь, упаковка и отправка данных, запуск на сервере, проверка |
| `deploy/migrate_pack.py` | Собирает `nova-migration.zip`: `.env`, согласованная копия базы (SQLite backup API), `data/` (ключи, медиа чатов, сессия Telegram), `wa-bridge/auth`, cookies RC, идентичность туннеля Cloudflare |
| `deploy/install_server.sh` | На сервере: `prepare` (Docker, код, образ), `start [домен]` (импорт архива, запуск), `update`, `status`, `logs`, `backup` |
| `deploy/Dockerfile`, `entrypoint.sh` | Один контейнер: сервер (главный процесс), бот и WhatsApp-мост в циклах перезапуска. Часовой пояс `Asia/Tashkent` |
| `deploy/docker-compose.yml` | `nova` + один из способов доступа: `cloudflared-token` / `cloudflared-file` (тот же туннель, адрес не меняется) или `caddy` (свой HTTPS для домена, нужна A-запись) |
| `cloud_update.bat`, `cloud_logs.bat` → `deploy/cloud.py` | Обновление и логи с офисного компьютера по сохранённому ключу (`cloud.json`) |

## Где что лежит на сервере

- Код: `/opt/novahome` (ветка `claude/new-session-85xp4a`, обновляется `git fetch` + `reset`).
- Данные: `/opt/novahome-data` → в контейнере `/data`: `nova_dashboard.db`, `.env`, `inbox_media/`,
  `wa-auth/`, `tg/`, `logs/`, `backups/` (ночные копии базы, 7 штук), `cloudflared/`.
  Обновления эту папку не трогают.
- Настройки запуска: `/opt/novahome/deploy/.env` (`COMPOSE_PROFILES`, `TUNNEL_TOKEN`, `CRM_DOMAIN`, `WA_BRIDGE`).

## Обновление

- Владелец пишет боту `/update` → бот кладёт `/data/update.request` → systemd-юнит
  `nova-update.path` запускает `install_server.sh flag` → новая версия собирается и
  стартует → результат в `/data/update.result`, сервер сообщает владельцам.
- Вручную: `bash /opt/novahome/deploy/install_server.sh update`.

## Вручную без move_to_cloud.bat

```
ssh root@IP
curl -fsSL https://raw.githubusercontent.com/timurisnotbad/novahomedashboardminiapp/claude/new-session-85xp4a/deploy/install_server.sh -o install_server.sh
bash install_server.sh prepare
# с компьютера: python deploy\migrate_pack.py ; scp nova-migration.zip root@IP:/root/
bash install_server.sh start            # туннель из архива; или: start crm.ваш-домен
```

Две копии бота одновременно работать не могут: на офисном компьютере после переезда
лежит `MOVED_TO_CLOUD.txt`, и `restart_all.bat` ничего не запускает.
