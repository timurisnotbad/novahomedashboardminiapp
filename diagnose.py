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

PROBLEMS: list[str] = []          # что именно чинить — печатается в конце
REPORT: list[str] = []            # весь отчёт, чтобы сохранить в файл


def problem(text: str) -> None:
    PROBLEMS.append(text)


_real_print = print


def print(*args, **kwargs):  # noqa: A001 — весь вывод дублируется в файл
    text = " ".join(str(a) for a in args)
    REPORT.append(text)
    _real_print(*args, **kwargs)


def line(title=""):
    print("\n" + title)
    print("-" * 68)


def check_python():
    line("1. PYTHON")
    v = sys.version_info
    print(f"{OK if v >= (3, 10) else BAD}Версия {v.major}.{v.minor}.{v.micro}"
          + ("" if v >= (3, 10) else "  — нужна 3.10 или новее"))
    if v < (3, 10):
        problem("Обновите Python до 3.11 (python.org), галочка «Add python.exe to PATH»")
    print(f"         {sys.executable}")


def check_packages():
    line("2. БИБЛИОТЕКИ")
    need = [("fastapi", "сервер"), ("uvicorn", "сервер"), ("requests", "запросы"),
            ("apscheduler", "расписание сервера"), ("telegram", "бот"),
            ("pytz", "нужен только старой версии бота (необязательно)"),
            ("playwright", "вкладка «Цены» (необязательно)")]
    missing = []
    for mod, what in need:
        try:
            m = __import__(mod)
            ver = str(getattr(m, "__version__", "") or "")
            print(f"{OK}{mod:<14} {ver:<10} — {what}")
        except BaseException:  # noqa: BLE001 — a broken C-extension can raise outside Exception
            optional = mod in ("playwright", "pytz")
            print(f"{WARN if optional else BAD}{mod:<14} {'':<10} — {what}"
                  + ("  (не установлен)" if optional else "  ← НЕ УСТАНОВЛЕН"))
            if not optional:
                missing.append(mod)
    if missing:
        print("\n  Исправить:  запустите install.bat")
        problem("Не установлены библиотеки: " + ", ".join(missing) + " — запустите install.bat")
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
            print("         Исправить:  запустите install.bat")
            problem("Нет планировщика: перекличка 14:00 и контроль 18:00 не работают — install.bat")
    except BaseException as exc:  # noqa: BLE001
        print(f"{BAD}Не удалось проверить: {type(exc).__name__}: {exc}")
        problem(f"Библиотека бота сломана ({type(exc).__name__}) — запустите install.bat")


def check_env():
    line("4. ФАЙЛ .env")
    p = BASE / ".env"
    if not p.exists():
        print(f"{BAD}Файла .env нет. Скопируйте .env.example в .env и заполните.")
        problem("Нет файла .env — скопируйте .env.example в .env и заполните")
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
    hints = {
        "BOT_TOKEN": "BOT_TOKEN пуст — бот не запустится. Впишите токен от @BotFather",
        "WEBAPP_URL": "WEBAPP_URL пуст — кнопка «Дашборд» не откроется",
        "OWNER_IDS": "OWNER_IDS пуст — вы не опознаётесь как владелец",
        "OWNER_KEY": "OWNER_KEY пуст — на Mac и в веб-версии откроется режим сотрудника. "
                     "Впишите в .env строку OWNER_KEY=любой-длинный-секрет",
    }
    for name, val, required in rows:
        mark = OK if val else (BAD if required else WARN)
        print(f"{mark}{name:<16} {shown(val)}")
        if required and not val and name in hints:
            problem(hints[name])
    if config.DEMO_MODE:
        print(f"{WARN}DEMO_MODE включён — показываются придуманные брони, не настоящие")
        problem("DEMO_MODE включён или RC_TOKEN пуст — данные ненастоящие")
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
        problem("Сервер не запущен — запустите restart_all.bat")
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
        if not same:
            problem(f"Сервер работает на версии {data.get('version')}, а файлы версии "
                    f"{config.APP_VERSION} — закройте окна и запустите restart_all.bat")
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
        print(f"{WARN}Папки logs ещё нет — значит, бот и сервер новой версии ещё не запускались")
        problem("Бот ещё не запускался после обновления — запустите start_bot.bat")
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

    line("ИТОГ")
    if PROBLEMS:
        print(f"  Найдено проблем: {len(PROBLEMS)}\n")
        for i, t in enumerate(PROBLEMS, 1):
            print(f"  {i}. {t}")
    else:
        print("  Проблем не найдено. Если что-то не работает — пришлите логи из папки logs.")
    print("\n" + "=" * 68)
    saved = save_report()
    if saved:
        print(f"  Отчёт целиком сохранён в файл:\n    {saved}")
        print("  Пришлите этот файл — прокручивать окно не нужно.")
    print("=" * 68)


def save_report():
    try:
        logs = BASE / "logs"
        logs.mkdir(exist_ok=True)
        path = logs / "diagnose.txt"
        path.write_text("\n".join(REPORT), encoding="utf-8")
        return path
    except BaseException:  # noqa: BLE001
        return None


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
