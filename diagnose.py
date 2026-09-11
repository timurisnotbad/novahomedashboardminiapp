"""Самопроверка Nova Home. Запускается двойным кликом по diagnose.bat.

Печатает отчёт по-русски: что установлено, что в .env, работает ли сервер,
последние ошибки из логов. Скриншот этого отчёта отвечает почти на любой
вопрос «почему не работает».
"""
import os
import socket
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

OK, BAD, WARN = "  [OK]   ", "  [НЕТ]  ", "  [!]    "


def line(title=""):
    print("\n" + title)
    print("-" * 68)


def check_python():
    line("1. PYTHON")
    v = sys.version_info
    print(f"{OK if v >= (3, 10) else BAD}Версия {v.major}.{v.minor}.{v.micro}"
          + ("" if v >= (3, 10) else "  — нужна 3.10 или новее"))
    print(f"         {sys.executable}")


def check_packages():
    line("2. БИБЛИОТЕКИ")
    need = [("fastapi", "сервер"), ("uvicorn", "сервер"), ("requests", "запросы"),
            ("apscheduler", "расписание сервера"), ("pytz", "расписание бота"),
            ("telegram", "бот"), ("playwright", "вкладка «Цены» (необязательно)")]
    missing = []
    for mod, what in need:
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "")
            print(f"{OK}{mod:<14} {ver:<10} — {what}")
        except BaseException:  # noqa: BLE001 — a broken C-extension can raise outside Exception
            optional = mod == "playwright"
            print(f"{WARN if optional else BAD}{mod:<14} {'':<10} — {what}"
                  + ("  (не установлен)" if optional else "  ← НЕ УСТАНОВЛЕН"))
            if not optional:
                missing.append(mod)
    if missing:
        print("\n  Исправить:  pip install -r backend\\requirements.txt")
    return missing


def check_jobqueue():
    line("3. РАСПИСАНИЕ БОТА (перекличка 14:00, контроль 18:00, автозакрытие)")
    try:
        from telegram.ext import Application
        app = Application.builder().token("1:AA").build()
        if app.job_queue:
            print(f"{OK}Планировщик бота работает")
        else:
            print(f"{BAD}Планировщик НЕ создан — задачи по времени выполняться не будут")
            print("         Исправить:  pip install \"python-telegram-bot[job-queue]\" pytz")
    except BaseException as exc:  # noqa: BLE001
        print(f"{BAD}Не удалось проверить: {type(exc).__name__}: {exc}")


def check_env():
    line("4. ФАЙЛ .env")
    p = BASE / ".env"
    if not p.exists():
        print(f"{BAD}Файла .env нет. Скопируйте .env.example в .env и заполните.")
        return
    print(f"{OK}Найден: {p}")
    try:
        from backend import config
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}Ошибка чтения настроек: {type(exc).__name__}: {exc}")
        return
    def shown(v):
        return "задан" if v else "ПУСТО"
    rows = [
        ("BOT_TOKEN", bool(config.BOT_TOKEN), True),
        ("WEBAPP_URL", bool(config.WEBAPP_URL), True),
        ("OWNER_IDS", bool(config.OWNER_TELEGRAM_IDS), True),
        ("OWNER_KEY", bool(config.OWNER_KEY), True),
        ("RC_TOKEN", bool(config.RC_TOKEN), False),
        ("NOTIFY_CHAT_IDS", bool(config.NOTIFY_CHAT_IDS), False),
        ("PAY_CHANNEL_IDS", bool(config.PAY_CHANNEL_IDS), False),
        ("SHEET_API_URL", bool(config.SHEET_API_URL), False),
    ]
    for name, val, required in rows:
        mark = OK if val else (BAD if required else WARN)
        print(f"{mark}{name:<16} {shown(val)}")
    if config.DEMO_MODE:
        print(f"{WARN}DEMO_MODE включён — показываются придуманные брони, не настоящие")
    print(f"         Версия приложения в файлах: {config.APP_VERSION}")
    print(f"         Перекличка в {config.ATTEND_DEADLINE_T[0]:02d}:{config.ATTEND_DEADLINE_T[1]:02d}, "
          f"контроль уборок в {config.CLEANING_CHECK_T[0]:02d}:{config.CLEANING_CHECK_T[1]:02d}")


def check_db():
    line("5. БАЗА ДАННЫХ")
    try:
        from backend import config, database
        database.init_db()
        p = Path(config.DB_PATH)
        size = p.stat().st_size / 1024 if p.exists() else 0
        print(f"{OK}{p}  ({size:.0f} КБ)")
        with database.get_conn() as conn:
            for table, label in (("bookings", "броней"), ("cleaning_sessions", "уборок"),
                                 ("attendance", "отметок явки"), ("supplies", "заявок на закупку")):
                try:
                    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    print(f"         {label}: {n}")
                except Exception:  # noqa: BLE001
                    print(f"{WARN}таблица {table} недоступна")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}{type(exc).__name__}: {exc}")


def check_server():
    line("6. СЕРВЕР (порт 8000)")
    busy = False
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=2):
            busy = True
    except OSError:
        pass
    if not busy:
        print(f"{BAD}Сервер не отвечает на localhost:8000 — окно «Nova Backend» не запущено")
        return
    print(f"{OK}Порт 8000 занят — сервер запущен")
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5) as r:
            data = json.loads(r.read().decode())
        from backend import config
        same = str(data.get("version")) == str(config.APP_VERSION)
        print(f"{OK if same else BAD}Версия сервера: {data.get('version')} "
              f"(файлы: {config.APP_VERSION})" + ("" if same else "  ← СТАРЫЙ ПРОЦЕСС, перезапустите"))
        print(f"         Последняя синхронизация: {data.get('last_sync')}")
    except Exception as exc:  # noqa: BLE001
        print(f"{WARN}/api/health не ответил: {type(exc).__name__}: {exc}")


def tail(path: Path, n: int = 12):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return []
    return lines[-n:]


def check_logs():
    line("7. ПОСЛЕДНИЕ ОШИБКИ ИЗ ЛОГОВ")
    logs = BASE / "logs"
    if not logs.exists():
        print(f"{WARN}Папки logs ещё нет (появится после запуска новой версии)")
        return
    found = False
    for f in sorted(logs.glob("*.log")):
        rows = tail(f, 40)
        bad = [r for r in rows if "ERROR" in r or "Traceback" in r or "Exception" in r]
        if bad:
            found = True
            print(f"\n  {f.name}:")
            for r in bad[-8:]:
                print("    " + r[:160])
    if not found:
        print(f"{OK}Ошибок в логах нет")
    print(f"\n         Полные логи: {logs}")


def main():
    print("=" * 68)
    print("  NOVA HOME — САМОПРОВЕРКА")
    print("  Папка: " + str(BASE))
    print("=" * 68)
    check_python()
    check_packages()
    check_jobqueue()
    check_env()
    check_db()
    check_server()
    check_logs()
    print("\n" + "=" * 68)
    print("  Готово. Сделайте скриншот этого окна целиком и пришлите его.")
    print("=" * 68)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001
        import traceback
        print("\nСама проверка упала с ошибкой:")
        traceback.print_exc()
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\nНажмите Enter, чтобы закрыть окно… ")
    except Exception:  # noqa: BLE001
        pass
