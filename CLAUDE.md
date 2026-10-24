# Nova Home Dashboard — passport for a new session

## What this is
Telegram bot + web CRM for Nova Home Apartments (Nest One, Tashkent, 17 apartments, owner Timur).
Repo `timurisnotbad/novahomedashboardminiapp`, work branch `claude/fervent-planck-gno50t` (push there; no PR unless asked).
Current release **v100**. Owner installs releases from a zip (see «Release»), server is Windows («Nova Backend» window = uvicorn, «Nova WhatsApp» = Baileys bridge, `restart_all.bat`).
Language of the product and of the owner: Russian. Never put model names into commits/code. Keys live only in the owner's `.env`; `data/` is gitignored.

## Code map
- `backend/main.py` FastAPI app, lifespan starts everything (`*.init_db()`, scheduler, `wazzup.start`, `tg_channels.start/bot_start`, `crm_bot.start`), `_HTML_PAGES` stamps `?v=NN` and `window.NH_VERSION` into html.
- `backend/config.py` env → settings, `APP_VERSION`, `APARTMENTS`, `OWNER_TELEGRAM_IDS` (OWNER_IDS), `BOT_TOKEN`, `CRM_BOT_TOKEN` (+`crm_bot_token()`), `PAY_CHANNEL_IDS`, `SHEET_API_URL/TOKEN` (`FINANCE_ENABLED`).
- `backend/inbox.py` shared inbox (chats/messages SQLite): `send()`, `_dispatch_send()` per channel (wa bridge / wz Wazzup / wac / ig / tg Telethon account / tgbot guest bot / bk Booking extension), `edit_sent/delete_sent` (v98), `on_missed_call` + `guest_language` (v99), `_stays_for_chat` (v96), `_alert` (Telegram alerts with reply map `inbox_tg_map`), `notify_pending/_notify_chat` (unanswered chats → push + personal Telegram), `resolve_user` (dual auth: CRM cookie / personal key `?k=` / Telegram initData / owner key), `register_lead`, edits (`_apply_edit`, `edited_at`).
- `backend/routers/inbox.py` `/api/inbox/*` (`inbox_user` dep), bridge hook `/api/inbox/hook` (events message/history/contacts/edit/ack/status/call).
- `backend/routers/crm.py` `/api/crm/*` cookie auth (`current_user`, `admin_user`); integrations settings (`SettingsIn`), `/me/telegram*` (link code, prefs).
- `backend/routers/cash.py` `/api/cash/*` (v94), `backend/cash.py` payments → Google Sheet «Операции» via Apps Script (`deploy/apps-script-cash.gs`).
- `backend/alerts.py` (v98) per-user Telegram notification kinds (messages/tasks/bookings/ai/cash/system), `broadcast(kind, text, base)`, `task_reminders()`; `backend/crm_bot.py` (v100) second bot long-polling thread (/start, /link, /chats, /cash, reply→guest).
- `backend/notify.py` Telegram sender (`send(text, targets, topic, token=None)`); `backend/scheduler.py` APScheduler jobs (ai_followups 10 min, cash_retry 10 min, task_alerts 5 min, task messages 5 min…).
- `backend/ai.py` sales assistant (Anthropic): per-chat modes, tools check_availability/send_photos/save_lead/handover, lessons, style, follow-ups; `backend/rc_rates.py` nightly prices from RealtyCalendar; `backend/crm.py` CRM core (users, clients, deals, tasks, steps «Этапы заезда», settings), `backend/crm_ext.py` auto-rules, step messages (`run_task_messages`, gated by `auto_step_messages`), `guest_lang`, `unresolved()`.
- `backend/tg_channels.py` Telethon account channel + guest bot (`user_send/user_edit/user_delete`, `bot_send/bot_edit/bot_delete`, albums, `_on_edited`). `backend/wazzup.py`, `backend/meta_api.py`, `backend/booking_ext.py`.
- `bot.py` dashboard Telegram bot (python-telegram-bot v20): cleaning reports, attendance, `/chats`, `/cash` `/оплата` (v94: WebApp buttons, quick text «110 400$ 480 000 19.10-23.10» → preview → ✅), `on_channel_payment` (PAY_CHANNEL_IDS posts → `cash.add`), `/link` (redirects to CRM bot when CRM_BOT_TOKEN set).
- `wa-bridge/bridge.mjs` Baileys bridge: `/send`, `/edit`, `/delete`, `/read`, `/logout`; posts events to the hook incl. edits (protocolMessage) and `call` (missed/rejected calls, v99).
- Frontend (vanilla JS): `frontend/crm/` (crm.js ~1700 lines, routes by hash; `#messages` and `#cash` are kept-alive iframes `.inbox-frame`), `frontend/inbox/` (chats page, also standalone from bot), `frontend/cash/` (v94 payments page, CRM look), `frontend/js/common.js` (request helper, esc, dates), `frontend/js/keyboard.js` (v95 phone keyboard handling), `frontend/js/phone.js` (tel masks). CRM is installed as iOS home-screen app (standalone).
- Docs: `docs/CHANGELOG.md` (every release, top = newest), `README.md`, `.env.example`, `deploy/apps-script-cash.gs`.

## Done in this project line (v83 → v100)
v83 RC tariffs for AI · v84–v86 AI quality (per-chat toggle, Uzbek, voice transcription, playbook, lessons, lost-context fix) · v87 apartment photos + send_photos · v88 AI tab rebuilt · v89 only manual tasks shown, auto tasks off · v90 speed (immutable assets) · v91 background drafts · v92 every lead registered (username/phone) · v93 edited messages reflected; home tiles · v94 «Оплаты» (CRM page, bot, sheet rows, joint payments → review, audit, cancel = amount 0) · v95 keyboard module · v96 chat list shows all stays with dates; keyboard applies only on change · v97 step messages to guests OFF by default (`auto_step_messages`), «...» counts as unfilled · v98 edit/delete own messages (wa/tg/tgbot); per-user Telegram notification kinds · v99 missed WhatsApp calls → «📞» line + auto-reply in guest language (`missed_call_*` settings, Интеграции → вкладка «Сценарий заезда») · v100 separate CRM notification bot (`CRM_BOT_TOKEN`).

## Decisions taken with the owner
- Sheet «Операции» mirrors real cash per apartment: rows only for the receiver apartment, bills never split; joint payments wait in owner's review («Оставить» / «Записать как долг между квартирами» → sheet «Долг между Аппартами»). Article is always «Бронь». Stay dates typed manually («19.10-23.10» → comment «19-23.10»). Rates/accounts from sheet «Валюты» (cached 1 h, defaults in `cash.DEFAULT_*`). Write to the sheet immediately; failures retried every 10 min. Rows with year 1899 = no date (legacy). Open question: column «Сумма $» value or formula — CRM writes a number; `USD_FORMULA` in the script switches to a formula.
- Nothing is deleted in payments: edit rewrites the same rows (author 30 min, owner always), cancel needs a reason, audit `cash_audit`, owner notified.
- CRM never messages guests automatically unless the owner enables it (`auto_step_messages`, default 0). Auto-rules (crm_auto_rules) are seeded OFF.
- AI replies are labelled, never impersonate a person, test toggle per chat, default off.
- One notification copy per person: linked CRM users get it from the CRM bot; the dashboard bot keeps work groups / owner ids that are not linked.
- Everything on the phone: big controls, few buttons (owner's words: «достать, открыть, записать и всё»).

## Current problem (open)
Owner's new CRM bot («NOVA CRM») did not answer /start and /link after v100. Not reproducible here (api.telegram.org is blocked from this sandbox, token validation impossible). Locally the thread starts and logs `CRM bot @… is polling` or `CRM bot: <error>` (`backend/crm_bot.py::_loop`). Owner was asked to: put `CRM_BOT_TOKEN=` into `.env` (it was shipped only in `env.add` inside the zip), restart via `restart_all.bat`, read `logs/backend.log`, check CRM → «Telegram-уведомления» (shows bot name or `bot_error`). Likely causes: token not in `.env`/no restart, or 409 (webhook set / another poller). Owner's Telegram id 36201702 must be in OWNER_IDS.
Also unverified on a real device: iOS keyboard handling (v95/v96, `js/keyboard.js`; 5 taps on the CRM title show diagnostics), WhatsApp edit/delete through the bridge (needs the bridge running), missed-call events (needs bridge restart), the Apps Script in the owner's sheet (not installed yet as far as known).

## Run / test / release
- Server for UI checks: `DB_PATH=<copy of a real db> INBOX_MEDIA_DIR=<dir> BOT_TOKEN= RC_TOKEN= ANTHROPIC_API_KEY=sk-ant-fake-for-ui CRM_ADMIN_EMAIL=admin@novahome.uz CRM_ADMIN_PASSWORD=secret123 python3 -m uvicorn backend.main:app --port 8000`. Kill by scanning `/proc` for `python3 -m uvicorn backend.main` (never pkill). Delete `data/secrets.json` after test runs (created automatically).
- Tests are plain scripts in `tests/session/` (copied from the session scratchpad): `DB_PATH=<db copy> BOT_TOKEN= RC_TOKEN= python3 tests/session/test_cash.py` etc. They need a real-shaped DB (the owner's DB copy was used; not in repo). Playwright scripts `tests/session/ui_*.mjs` run with `SP=<dir> node ui_x.mjs` against the local server (Chromium at `/opt/pw-browsers/chromium-1194/chrome-linux/chrome` in the sandbox; login `#auth-email/#auth-pass/#auth-btn`). `pip install python-telegram-bot==20.8` is needed to import `bot.py`.
- Release: bump `APP_VERSION` in `backend/config.py`, `window.NH_VERSION || "NN"` in `frontend/js/api.js`, `?v=NN` in all `frontend/**/*.html`; changelog entry on top of `docs/CHANGELOG.md`; commit; `git push -u origin claude/fervent-planck-gno50t`; zip = `git archive HEAD` extracted into `novahomedashboard/` + the owner's `env.add` (env additions, kept outside the repo) → `nova-updateNN.zip`, sent to the owner in chat.
- Sandbox limits: docs.google.com and api.telegram.org are unreachable from the session; Google Sheet work was done from screenshots.

## Next steps
1. Get the CRM bot alive (see problem above); then owner re-links via `/link КОД` in the new bot.
2. Owner installs `deploy/apps-script-cash.gs` into the sheet; verify the first payment row; settle «Сумма $» value vs formula.
3. Confirm on the owner's iPhone: keyboard never covers the composer (CRM standalone app); if not, read the 5-tap diagnostics and adjust `js/keyboard.js`.
4. Verify WhatsApp edit/delete and missed-call auto-reply with the bridge running; owner turns off WhatsApp Business's own away/missed-call message.
5. Ideas not started: payments stats by apartment, native mobile app (Telegram bots are the stop-gap), Wazzup/Meta have no edit/delete API.
