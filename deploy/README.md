# Nova Home на отдельном сервере (Linux VPS)

Вся система — дашборд, бот, CRM, чаты, Wazzup — переезжает с офисного компьютера на
сервер, который работает круглосуточно. Заходить можно с любого устройства по
`https://crm.ваш-домен/crm/`.

## Что нужно

1. **VPS** с Ubuntu 22.04/24.04: 1–2 CPU, 2 ГБ RAM, 20 ГБ диск. Подойдут Timeweb Cloud,
   Hetzner, Selectel, DigitalOcean — ~$5–8/мес. При заказе выбрать Ubuntu и добавить
   SSH-ключ или получить root-пароль.
2. **Домен/поддомен**, например `crm.novahomedashboard.com`: в DNS создать A-запись на IP
   сервера. Если DNS в Cloudflare — оранжевое облако можно оставить.

## Установка — одна команда

Подключиться к серверу (`ssh root@IP`) и выполнить:

```
curl -fsSL https://raw.githubusercontent.com/timurisnotbad/novahomedashboardminiapp/main/deploy/install_server.sh | sudo bash -s -- crm.novahomedashboard.com
```

Скрипт поставит Docker, скачает проект в `/opt/novahome`, создаст `.env`, соберёт и
запустит всё с автоматическим HTTPS-сертификатом.

Затем вписать ключи: `nano /opt/novahome/.env` (BOT_TOKEN, OWNER_IDS, OWNER_KEY, RC_TOKEN,
WAZZUP_API_KEY…) и перезапустить:

```
cd /opt/novahome/deploy && docker compose up -d
```

## Перенос данных с офисного компьютера

Чтобы не потерять базу, чаты и привязки, скопировать на сервер:

| На компьютере | На сервере (внутри тома `nova-data`) |
|---|---|
| `nova_dashboard.db` | `/var/lib/docker/volumes/deploy_nova-data/_data/nova_dashboard.db` |
| папка `data\inbox_media` | `…/_data/inbox_media/` |
| папка `wa-bridge\auth` (QR-сессия WhatsApp) | `…/_data/wa-auth/` |
| `.env` | `/opt/novahome/.env` (поменять `WEBAPP_URL` на новый домен) |

Копировать при остановленном контейнере: `docker compose stop`, скопировать (WinSCP
или `scp`), `docker compose up -d`.

После переезда на офисном компьютере закрыть окна Nova — две копии бота одновременно
работать не могут (Telegram отдаёт обновления только одному).

## Обновление

Повторить ту же команду установки — она подтянет новую версию и перезапустит.

## Команды

```
cd /opt/novahome/deploy
docker compose logs -f nova        # живой лог сервера, бота и моста
docker compose restart nova        # перезапуск
docker compose exec nova python diagnose.py   # самопроверка
```

Резервная копия базы: `docker cp deploy-nova-1:/data/nova_dashboard.db ./backup.db`
