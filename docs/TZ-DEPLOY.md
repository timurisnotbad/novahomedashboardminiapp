# Техническое задание: Nova Home Dashboard

**Версия продукта:** 93 (19.10.2026) · **Архив:** `nova-update93.zip` · **Статус:** готов к развёртыванию и настройке

Документ сверен с исходным кодом v93. Расхождения с ранее составленным описанием по v92 отмечены в конце (раздел 17).

---

## 1. Что это

Система управления апартаментами посуточной аренды (Nova Home, Nest One, Ташкент). Один сервер на Python/FastAPI
с SQLite, рядом Telegram-бот для команды и Node.js-мост WhatsApp. Три интерфейса: Telegram Mini App (дашборд для
сотрудников), CRM в браузере (`/crm/`, PWA), страница чатов (`/inbox/`, встроена в CRM и доступна отдельно).

Что делает:
- каждые 15 минут синхронизирует брони из RealtyCalendar, раз в час — тарифы (цены за ночь) из шахматки;
- уборки: «до/после» с фото от горничных через бота, контроль, автозакрытие;
- явка сотрудников по live-геолокации, перекличка, автоштрафы;
- CRM: клиенты, сделки (канбан), задачи, воронки с автодействиями, документы PDF, выручка;
- единый инбокс всех каналов (WhatsApp через Wazzup или QR-мост, Telegram-аккаунт, гостевой Telegram-бот,
  Instagram, Booking.com через расширение);
- ИИ-ассистент (Claude): триаж дешёвой моделью, ответ основной, инструменты (шахматка с ценами, фото, заявка,
  время приезда, передача человеку), база опыта, дожим молчащих, расшифровка голосовых (OpenAI);
- автосообщения гостям по сценарию заезда (RU+EN), мониторинг цен Booking.com.

## 2. Стек

| Компонент | Технология |
|---|---|
| Backend | Python 3.11+, FastAPI, uvicorn, APScheduler |
| База | SQLite, один файл (`DB_PATH`, по умолчанию `nova_dashboard.db`), WAL |
| Бот | python-telegram-bot v20 |
| Telegram-аккаунт компании | Telethon |
| WhatsApp QR-мост | Node.js LTS + Baileys (`wa-bridge/`) |
| Frontend | чистый JS без фреймворков: `frontend/` (Mini App), `frontend/crm/`, `frontend/inbox/` |
| ИИ | Anthropic Claude API (`anthropic` SDK), модели Opus 5.5 / Sonnet 5.5 / Haiku 5.5 |
| Голосовые | OpenAI transcription API (`gpt-4o-mini-transcribe`, запасной `whisper-1`) |
| Цены Booking | Playwright + Chromium |
| PDF | ReportLab, шрифты DejaVu в `backend/fonts/` |
| HTTPS | Cloudflare Tunnel (Windows) или Caddy в Docker (Linux) |

## 3. Структура проекта

```
novahomedashboard/
├── bot.py                  Telegram-бот команды
├── backend/
│   ├── main.py             FastAPI, lifespan, роутеры, кэш статики, первичная синхронизация
│   ├── config.py           конфигурация из .env и data/secrets.json, APP_VERSION
│   ├── database.py         схема SQLite, брони, уборки
│   ├── crm.py              пользователи, клиенты, сделки, задачи, квартиры, лиды
│   ├── crm_ext.py          сценарий заезда, этапы, автодействия, долги
│   ├── crm_amo.py          сделки из чатов (воронка «Входящие»)
│   ├── inbox.py            единый инбокс: чаты, сообщения, отправка, альбомы, регистрация лидов
│   ├── ai.py               ИИ-ассистент; ai_playbook.py — методика продаж по умолчанию
│   ├── rc_sync.py / rc_push.py / rc_rates.py   чтение броней, запись в календарь, тарифы
│   ├── tg_channels.py      Telegram-аккаунт (Telethon) и гостевой бот
│   ├── wazzup.py, meta_api.py, booking_ext.py  каналы Wazzup, Meta (WhatsApp Cloud + Instagram), Booking.com
│   ├── scheduler.py        фоновые задания сервера
│   ├── reminders.py, notify.py, webpush.py     автосообщения и уведомления
│   ├── docs.py             PDF-документы, реквизиты компании
│   ├── attendance.py, supplies.py, services.py, booking_prices.py, pay_parse.py, sheets.py
│   ├── auth.py, secrets_store.py, netsafe.py, util.py, logsetup.py, bg.py, watchdog.py
│   └── routers/            crm.py, inbox.py, dashboard.py, bookings.py, cleaning.py, occupancy.py,
│                           finance.py, tasks.py, prices.py, penalties.py, payroll.py, payrecon.py,
│                           control.py, payments.py, sync.py
├── frontend/
│   ├── index.html, js/app.js, js/api.js, js/common.js, js/phone.js, js/screens/*.js, css/style.css
│   ├── crm/  index.html, crm.js, crm.css, picker.js, sw.js, manifest.json, icon.svg
│   └── inbox/ index.html, inbox.js, inbox.css
├── wa-bridge/ bridge.mjs, package.json
├── booking-ext/ Chrome-расширение для экстранета Booking.com
├── deploy/ Dockerfile, docker-compose.yml, Caddyfile, entrypoint.sh, install_server.sh, README.md
├── docs/ CHANGELOG.md, AUDIT-2026-10-12.md, RECOMMENDATIONS.md, AI-SALES-PLAYBOOK.md, TZ-DEPLOY.md, инструкции горничным
├── .env.example, setup_env.py, install.bat, restart_all.bat, start_server.bat, start_bot.bat, start_wa.bat,
│   autostart.bat, diagnose.bat, diagnose.py, README.md
└── data/ (не в git): secrets.json, inbox_media/, wa-bridge/auth/
```

## 4. База данных

Схема создаётся и мигрируется автоматически при старте (`init_db()` каждого модуля). Основные таблицы:

| Таблица | Назначение |
|---|---|
| `bookings`, `bookings_missing`, `bookings_cancelled`, `booking_snapshots`, `notified_bookings` | брони из RealtyCalendar и их история |
| `rc_rates` | цены за ночь по квартирам и дням из шахматки |
| `cleaning_sessions`, `cleaning_status` | уборки |
| `staff_registry`, `attendance`, `penalties`, `supplies`, `salary_payments`, `payments_cash`, `channel_payments` | сотрудники, явка, штрафы, закупки, деньги |
| `crm_users`, `crm_clients`, `crm_deals`, `crm_tasks`, `crm_notes`, `crm_fields`, `crm_settings` | CRM |
| `crm_pipelines`, `crm_stages`, `crm_stage_actions` | воронки, этапы, автодействия |
| `crm_apartment_info`, `crm_apartment_photos` | описания квартир (домофон, ключи, Wi-Fi, описание для гостей) и фото |
| `crm_booking_checklist`, `crm_auto_rules`, `crm_auto_log`, `crm_eta` | сценарий заезда, автосообщения, время приезда |
| `crm_booking_chats`, `crm_deal_chats`, `crm_documents`, `crm_emails`, `crm_rc_log`, `crm_push_subs` | связи и журналы |
| `inbox_chats`, `inbox_messages`, `inbox_templates`, `inbox_agents`, `inbox_tg_map`, `inbox_ext_*`, `wazzup_chats` | инбокс |
| `ai_log`, `ai_examples` (+FTS5), `ai_lessons` | ИИ: журнал, примеры, уроки |
| `tasks`, `task_notifications`, `job_runs`, `sync_log`, `access_keys`, `chat_topics`, `bot_messages` | служебные |

В `inbox_chats` с v92 есть `username` (@handle), с v86–v88 — `ai_mode`, `ai_draft`, `ai_draft_photos`, `ai_last_in_id`,
`ai_last_status`, `ai_followup_at`. В `crm_clients` — `telegram`, `instagram`, `phone2`, `ai_memory`.

## 5. Конфигурация (.env)

Полный шаблон — `.env.example`. Ключи ИИ можно вводить и через CRM: они сохраняются в `data/secrets.json`.

Обязательные: `BOT_TOKEN`, `OWNER_IDS`, `RC_TOKEN`, `WEBAPP_URL`.
ИИ: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (голосовые).
Каналы: `WAZZUP_API_KEY` (+`WAZZUP_CHANNEL_ID`), `WA_CLOUD_TOKEN`, `WA_CLOUD_PHONE_ID`, `META_VERIFY_TOKEN`,
`META_APP_SECRET`, `IG_PAGE_TOKEN`, `IG_ACCOUNT_ID`, `TG_API_ID`, `TG_API_HASH`, `TG_GUEST_BOT_TOKEN`.
Явка: `WORK_LAT` (по умолчанию 41.31255), `WORK_LNG` (69.27920), `WORK_RADIUS_M` (700), `SHIFT_START` 09:00,
`ATTEND_REMIND` 10:00, `ATTEND_EARLIEST`, `ATTEND_DEADLINE` 14:00, `AUTO_FINE_NOSHOW`.
Уборки: `SESSION_MAX_HOURS` 8, `CLEANING_CHECK` 18:00. Тишина: `QUIET_FROM`/`QUIET_TO`.
Уведомления: `NOTIFY_CHAT_IDS`, `INBOX_NOTIFY_CHAT_IDS`, `HEARTBEAT_URL_BOT`, `HEARTBEAT_URL_SERVER`.
Email: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`. Google Sheets: `SHEET_API_URL`, `SHEET_API_TOKEN`.
Прочее: `DB_PATH`, `INBOX_MEDIA_DIR`, `SYNC_INTERVAL_MINUTES` 15, `SYNC_DAYS_AHEAD` 60, `CRM_ADMIN_EMAIL`/`CRM_ADMIN_PASSWORD`
(первый администратор), `OWNER_KEY`, `PAY_CHANNEL_IDS`, `PAY_VIEWERS`, `DEMO_MODE` (без `RC_TOKEN` включается сам).

## 6. Модули

### 6.1 Telegram-бот команды (`bot.py`)
Команды: `/start`, `/today`, `/sync`, `/attendance`, `/staff`, `/staff_del`, `/staff_ok`, `/link <код>` (личные
уведомления CRM), `/prices` и `/цены [даты]`, `/reset` и `/сброс 103`, `/supplies` и `/нужно`, `/topic`, `/chats`,
`/chatid`, `/myid`. Горничные: «до 103» / «после 103» с фото, «нужно 103 …», live-геолокация.
Расписание бота: 08:00 сводка, 09:00 цены Booking на завтра, напоминание о явке в `ATTEND_REMIND`, перекличка в
`ATTEND_DEADLINE`, контроль уборок в `CLEANING_CHECK`, план на завтра вечером; каждые 30 мин автозакрытие уборок
старше `SESSION_MAX_HOURS`. Heartbeat бота пишется в `logs/bot-heartbeat.txt`; сервер и бот следят друг за другом,
внешний контроль — healthchecks.io.

### 6.2 Планировщик сервера (`backend/scheduler.py`)
RealtyCalendar каждые `SYNC_INTERVAL_MINUTES`; после каждой синхронизации — клиенты из броней, сделки из чатов, сценарий
заезда, тарифы (не чаще раза в час); напоминания по задачам каждые 10 мин; автосообщения каждые 5 мин; дожим ИИ
каждые 10 мин; вечерняя сводка 22:00; утренний отчёт; уборка журналов ежедневно 04:17.

### 6.3 Mini App (дашборд)
Открывается кнопкой из `/start`, авторизация по initData Telegram. Экраны: Сегодня, Завтра, Уборки, Занятость,
Задачи, Гости, Касса, Цены, Оплаты, Контроль, Сверка оплат. Роли: владелец (`OWNER_IDS`), сотрудник (`/staff`),
наблюдатель оплат (`PAY_VIEWERS`).

### 6.4 CRM (`/crm/`)
Вход по email + пароль (cookie-сессия). Разделы: Главная, Сообщения (инбокс во фрейме), Сделки, Клиенты, Задачи,
Брони (день, шахматка), Выручка, Автосообщения, Каналы, Интеграции (Подключения / Реквизиты / Сценарий заезда /
Уведомления / ИИ-ассистент), Быстрые команды, Поля карточек, Воронки, Пользователи. Роли admin / manager.
Первый вход: `/crm/` предлагает создать администратора, либо `CRM_ADMIN_EMAIL`/`CRM_ADMIN_PASSWORD` в `.env`.
Задачи: в списке и счётчиках только задачи людей и ИИ; автозадачи «Заезд/Выезд» выключены по умолчанию
(Интеграции → «Задачи из броней»), шаги сценария заезда видны в карточке брони и на странице «Автосообщения».
На телефоне оболочка живёт в видимой области экрана (visualViewport), клавиатура не закрывает поле ввода.

### 6.5 Инбокс (`backend/inbox.py`)
Ключи каналов: `wa` (QR-мост), `wz` / `wzig` / `wztg` (Wazzup: WhatsApp / Instagram / Telegram), `wac` (WhatsApp
Cloud API), `ig` (Instagram Direct через Meta), `tg` (Telegram-аккаунт), `tgbot` (гостевой бот), `bk` (Booking.com
через расширение). Если настроен Wazzup, QR-мост считается запасным и плашка «Нужен QR» не показывается.
Каждый входящий лид сразу получает карточку клиента: @username (Telegram/Instagram) в карточке и в шапке чата,
телефон — когда известен (WhatsApp сразу; Telegram — если гость открыл номер или добавил нас; визитка-контакт;
номер в тексте). Совпадение с известным гостем объединяет карточки. Кнопка «Собрать контакты из переписок»
(Интеграции → Подключения) и разовый проход при первом запуске v92+ обрабатывают старые чаты.
Фото квартир уходят гостю все (до 30): в Telegram альбомами по 10, в WhatsApp подряд.

### 6.6 ИИ-ассистент (`backend/ai.py`)
Режимы: `off` (по умолчанию; включается в шапке отдельного чата — режим теста), `draft`, `auto`. Триаж Haiku 5.5,
ответ Opus 5.5 (настройка модели и глубины обдумывания `ai_effort`, по умолчанию medium), кэширование стабильного
системного блока, лимит расходов в месяц, пауза после ответа человека, дожим молчащего гостя (`ai_followup_hours`,
днём, один раз). Инструменты: `check_availability` (свободные квартиры и цены из `rc_rates` в $ и сумах по курсу
`usd_rate`, описания и число фото), `send_photos`, `save_lead`, `set_arrival_time`, `handover`.
Контекст: правила, методика продаж (`ai_playbook.py`, редактируется), база знаний, памятка о стиле команды и
факты из переписок («Изучить переписки»), уроки (`ai_lessons`: из правок менеджера и 👎), память о госте
(`crm_clients.ai_memory`), примеры «вопрос → ответ» (FTS5), фото гостя, расшифровки голосовых.
Кнопка ✨ ставит задачу в фон; под шапкой чата видно «думает… / ответил / промолчал: причина / ошибка».
Цены моделей ($ за 1M токенов, вход/выход): Opus 5.5 4/20, Sonnet 5.5 2/10, Haiku 5.5 0.10/0.50.

### 6.7 RealtyCalendar
`rc_sync.py` — брони на `SYNC_DAYS_AHEAD` дней вперёд, раз в день прошлый год; окно-зеркало без удаления истории;
`rc_push.py` — запись гостя, сумм, дат, времени, заметки; `rc_rates.py` — тарифы на 180 дней с подбором рабочего
запроса календаря (кнопка «Загрузить тарифы», таблица попыток при неудаче).

### 6.8 Остальное
Уборки и явка — как в описании бота; документы PDF (`docs.py`), цены Booking (`booking_prices.py`), автосообщения
(`reminders.py`, правила в CRM), push в браузер (`webpush.py`), расширение Booking.com с догрузкой пропущенных чатов.

## 7. API (основные маршруты, все под `/api`)

Инбокс (cookie CRM или персональная ссылка): `GET /inbox/me`, `GET /inbox/chats?limit&before&only`, `GET /inbox/poll`,
`GET /inbox/chats/{id}` (чат + сообщения), `POST /inbox/chats/{id}/send`, `POST /inbox/chats/{id}/file`,
`PATCH /inbox/chats/{id}`, `POST /inbox/chats/{id}/ai` (режим), `POST|DELETE /inbox/chats/{id}/ai/draft`,
`POST /inbox/chats/{id}/ai/draft/send`, `POST /inbox/chats/{id}/ai/rate`, `POST /inbox/messages/{id}/transcribe`,
`GET /inbox/status`, `GET|POST /inbox/templates`.
CRM: `POST /crm/login`, `GET /crm/home`, `GET /crm/badges`, `GET|POST /crm/clients`, `GET|PUT|DELETE /crm/clients/{id}`,
`GET|POST /crm/deals`, `GET|PATCH|DELETE /crm/deals/{id}`, `POST /crm/deals/{id}/notes`, `GET|POST /crm/tasks`,
`PATCH|DELETE /crm/tasks/{id}`, `GET|POST /crm/pipelines`, `GET|POST /crm/fields`, `GET /crm/channels`,
`GET /crm/integrations`, `POST /crm/integrations/sync`, `GET /crm/rc/rates`, `POST /crm/rc/rates/sync`,
`GET /crm/rc/rates/quote`, `GET|POST /crm/apartments/info`, `GET /crm/apartments/photos`,
`POST /crm/apartments/{apt}/photos`, `POST /crm/leads/backfill`, `GET|POST /crm/ai/settings`, `GET /crm/ai/stats`,
`POST /crm/ai/learn`, `GET|POST /crm/ai/lessons`, `POST /crm/ai/test`.
Дашборд (initData Telegram): `GET /today`, `GET /tomorrow`, `GET /cleaning`, `GET /attendance`, `GET /supplies`,
`GET /occupancy`, `GET /control`, остальные — см. `backend/routers/`. Здоровье: `GET /health`.

## 8. Установка

Windows: распаковать, `copy .env.example .env` и заполнить (или `python setup_env.py`), `install.bat`,
`playwright install chromium` (для цен Booking), `restart_all.bat`, `autostart.bat`. Проверка: `/api/health`,
`/crm/`, `/start` боту. Обновление: распаковать новый архив поверх (`.env` и `data/` не трогаются), `restart_all.bat`.
Linux: `deploy/install_server.sh` или `docker compose up -d` в `deploy/` (сервер, бот, WhatsApp-мост, Caddy с HTTPS).

## 9. Диагностика
`diagnose.bat` → `logs/diagnose.txt`; логи `logs/server.log`, `logs/bot.log`, `logs/bot-error.log`; журнал ИИ в
CRM (Интеграции → ИИ-ассистент → Журнал); состояние каналов — страница «Каналы» (сбой одного канала показывается
в его карточке, страница не падает).

## 10. Особенности
Авторизация: Mini App по HMAC initData; CRM — cookie-сессия; чаты — cookie CRM или подписанная персональная ссылка.
Кэш: файлы с `?v=<версия>` — `immutable` на год, HTML — всегда проверяется; GZip. Секреты, введённые в CRM —
`data/secrets.json`. Ключи ИИ никогда не попадают в логи (redact).

## 11–13. Внешние сервисы, требования, бэкап
Как в исходном описании: RealtyCalendar и BotFather обязательны; Anthropic, OpenAI, Wazzup, Meta, Cloudflare Tunnel,
healthchecks.io, Google Sheets, Gmail SMTP — по необходимости. Сервер: Windows 10/11 без сна или Ubuntu 22.04 с
Docker. Бэкап: файл базы, `data/secrets.json`, `data/wa-bridge/auth/`, `data/inbox_media/`, `.env`.

## 14. Известные ограничения
Клиенты без пагинации (сотни — нормально, тысячи — медленно); медиа-каталог не чистится; тесты живут вне архива
(скрипты разработчика); QR-мост WhatsApp неофициальный; SQLite до ~10 000 гостей; телефон из Telegram виден только
если гость его открыл; тарифы из RealtyCalendar подбираются по списку вероятных запросов — при неудаче нужен адрес
запроса из DevTools.

## 15–16. Что нужно от заказчика и чек-лист
Без изменений, плюс после установки: Интеграции → Подключения → «Загрузить тарифы»; таблица квартир → описания и
фото; ИИ-ассистент → ключ, база знаний (чек-лист в подсказке поля), «Изучить переписки»; включить ИИ в одном чате в
режиме «черновик (тест)».

## 17. Исправления к описанию v92
- Таблицы: `crm_stages` (не `crm_pipeline_stages`), `crm_apartment_photos` (не `apartment_photos`), `staff_registry`
  (не `staff`); добавлены `rc_rates`, `crm_apartment_info`, `crm_auto_rules`, `crm_stage_actions`, `crm_booking_checklist`.
- Автозадачи «Заезд/Выезд» с v89 выключены по умолчанию, из списка задач и счётчиков автоматика убрана.
- Маршруты: сообщения чата — `GET /api/inbox/chats/{id}`, отправка — `POST /api/inbox/chats/{id}/send`
  (`/inbox/chats/{id}/messages` и `/inbox/send` не существуют); `PATCH /crm/deals/{id}` и `PUT /crm/clients/{id}` верны.
- Ключи каналов: WhatsApp Cloud — `wac`, Booking.com — `bk`.
- Координаты по умолчанию в коде: 41.31255 / 69.27920 / 700 м (в `.env` можно задать свои).
- Режим ИИ по умолчанию `off` (тест по чатам), а не `draft`; добавлены глубина обдумывания, дожим, уроки, факты,
  фото альбомами, фоновая генерация черновика.
- Регистрация лидов с @username и подхватом телефона (v92), сбор по старым чатам, клавиатура на телефоне (v93).
- Ограничение попыток входа в CRM в коде не подтверждено — пункт убран.
