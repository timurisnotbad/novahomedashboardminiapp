"""Move Nova Home from this computer to a cloud server — one guided run.

Started by move_to_cloud.bat. Asks for the server's IP address and (once) its
root password, then:

  1. creates an SSH key so the password is not asked again;
  2. prepares the server: Docker, the project, the image (5–10 minutes) —
     Nova keeps working here meanwhile;
  3. stops Nova on this computer and blocks its autostart (two copies of the
     bot cannot work at once);
  4. packs the database, chats, keys and the Cloudflare Tunnel identity and
     uploads them;
  5. starts Nova on the server and checks that it answers.

Nothing is deleted here: to come back, delete MOVED_TO_CLOUD.txt and run
restart_all.bat (see ПЕРЕЕЗД-В-ОБЛАКО.txt).
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RAW = "https://raw.githubusercontent.com/timurisnotbad/novahomedashboardminiapp/{branch}/deploy/install_server.sh"
BRANCH = os.environ.get("NOVA_BRANCH", "claude/new-session-85xp4a")
KEY = Path.home() / ".ssh" / "nova_cloud"
STATE = BASE / "cloud.json"
MARK = BASE / "MOVED_TO_CLOUD.txt"
SSH_OPTS = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30", "-o", "ConnectTimeout=20"]


def say(text: str = "") -> None:
    print(text, flush=True)


def step(n: int, text: str) -> None:
    say()
    say("=" * 64)
    say(f"  Шаг {n} из 5. {text}")
    say("=" * 64)


def stop(text: str) -> None:
    say()
    say("!!! " + text)
    say("Ничего не сломано: Nova на этом компьютере можно запустить restart_all.bat")
    say("(если появился файл MOVED_TO_CLOUD.txt — удалите его).")
    sys.exit(1)


def run(cmd: list[str], **kw) -> int:
    return subprocess.call(cmd, **kw)


def ssh(host: str, remote: str, key: bool = True, **kw) -> int:
    cmd = ["ssh", *SSH_OPTS]
    if key:
        cmd += ["-i", str(KEY), "-o", "BatchMode=yes"]
    return run(cmd + [f"root@{host}", remote], **kw)


def have(tool: str) -> bool:
    try:
        return subprocess.call(["where" if os.name == "nt" else "which", tool], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def tunnel_service_exists() -> bool:
    if os.name != "nt":
        return False
    return subprocess.call(["sc", "query", "Cloudflared"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def ensure_key(host: str) -> None:
    KEY.parent.mkdir(exist_ok=True)
    if not KEY.exists():
        if run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(KEY), "-q", "-C", "nova-home"]) != 0:
            stop("Не удалось создать ключ SSH (ssh-keygen).")
    if ssh(host, "echo ok", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        say("Ключ уже принят сервером — пароль не понадобится.")
        return
    say("Сейчас сервер спросит пароль root (тот, что прислал хостинг).")
    say("Пароль при вводе НЕ отображается — это нормально. Введите и нажмите Enter.")
    pub = (KEY.with_suffix(".pub")).read_text(encoding="utf-8").strip()
    remote = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    p = subprocess.Popen(["ssh", *SSH_OPTS, f"root@{host}", remote], stdin=subprocess.PIPE)
    p.communicate((pub + "\n").encode())
    if p.returncode != 0 or ssh(host, "echo ok", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        stop("Сервер не принял пароль или недоступен. Проверьте IP-адрес и пароль root и запустите ещё раз.")
    say("Готово: дальше пароль не нужен.")


def stop_local() -> list[str]:
    """Stop Nova here and make sure it does not start again by itself."""
    notes = []
    if os.name != "nt":
        return notes
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -ne 'powershell.exe' -and "
          "($_.CommandLine -like '*start_bot.bat*' -or $_.CommandLine -like '*start_server.bat*' -or $_.CommandLine -like '*start_wa.bat*') } "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
          "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -ne 'powershell.exe' -and "
          "($_.CommandLine -like '*backend.main:app*' -or $_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*bridge.mjs*') } "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    run(["powershell", "-NoProfile", "-Command", ps])
    notes.append("окна Nova Backend / Nova Bot / Nova WhatsApp закрыты")
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/NovaHome.bat"
    if startup.exists():
        try:
            startup.unlink()
            notes.append("автозапуск Nova на этом компьютере убран")
        except OSError as exc:
            notes.append(f"автозапуск не удалился ({exc}) — удалите файл {startup}")
    MARK.write_text("Nova Home переехала в облако. Пока этот файл лежит здесь, restart_all.bat ничего не запускает:\n"
                    "две копии бота одновременно работать не могут.\n"
                    "Вернуть работу на этот компьютер: удалить этот файл и запустить restart_all.bat.\n", encoding="utf-8")
    return notes


def stop_local_tunnel(mode: str) -> list[str]:
    """The tunnel now runs on the server; a second connector here would send
    half of the visitors to a computer where nothing answers."""
    notes = []
    if os.name != "nt" or mode == "none":
        return notes
    if tunnel_service_exists():
        a = run(["sc", "stop", "Cloudflared"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        b = run(["sc", "config", "Cloudflared", "start=", "disabled"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        notes.append("служба Cloudflare Tunnel на этом компьютере остановлена и отключена" if b == 0 else
                     "!!! службу Cloudflared не удалось отключить (нужны права администратора): "
                     "Пуск → Службы → Cloudflared → Остановить, тип запуска «Отключена»")
        _ = a
    run(["taskkill", "/IM", "cloudflared.exe", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if mode == "file":
        for d in (Path.home() / ".cloudflared",):
            for name in ("config.yml", "config.yaml"):
                p = d / name
                if p.exists():
                    try:
                        p.rename(p.with_name(p.name + ".moved-to-cloud"))
                        notes.append(f"{p.name} переименован, чтобы туннель не запустился здесь снова")
                    except OSError:
                        pass
    return notes


def check_public(url: str) -> str:
    if not url:
        return ""
    target = url.split("?")[0].rstrip("/") + "/api/health"
    for _ in range(12):
        try:
            with urllib.request.urlopen(target, timeout=10) as r:
                data = json.loads(r.read().decode())
                return f"{target} отвечает, версия {data.get('version')}"
        except Exception:  # noqa: BLE001
            time.sleep(5)
    return ""


def main() -> int:
    say("=" * 64)
    say("  NOVA HOME — переезд в облако")
    say("=" * 64)
    if not (BASE / ".env").exists():
        stop("Запускайте из рабочей папки Nova Home (рядом должны быть bot.py и .env).")
    if not have("ssh") or not have("scp"):
        stop("На компьютере нет программ ssh/scp. Windows 10/11: Параметры → Приложения → Дополнительные компоненты → "
             "«Клиент OpenSSH» → Установить, затем запустите снова.")
    if tunnel_service_exists() and not is_admin():
        stop("Запустите move_to_cloud.bat от имени администратора (правый клик → «Запуск от имени администратора»): "
             "нужно будет остановить службу Cloudflare Tunnel на этом компьютере.")

    host = ""
    if STATE.exists():
        try:
            host = json.loads(STATE.read_text(encoding="utf-8")).get("host", "")
        except Exception:  # noqa: BLE001
            host = ""
    entered = input(f"IP-адрес сервера{' [' + host + ']' if host else ''}: ").strip()
    host = entered or host
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        stop("Нужен IP-адрес вида 203.0.113.25 — он есть в письме от хостинга.")
    STATE.write_text(json.dumps({"host": host, "key": str(KEY)}, ensure_ascii=False, indent=2), encoding="utf-8")

    step(1, "Доступ к серверу")
    ensure_key(host)

    step(2, "Подготовка сервера (5–10 минут, Nova здесь пока работает)")
    url = RAW.format(branch=BRANCH)
    rc = ssh(host, f"curl -fsSL '{url}' -o /root/install_server.sh && NOVA_BRANCH='{BRANCH}' bash /root/install_server.sh prepare")
    if rc != 0:
        stop("Сервер не подготовился. Пришлите текст выше — по нему видно, на чём остановилось.")

    step(3, "Останавливаю Nova на этом компьютере")
    say("С этого момента и до конца шага 5 (2–4 минуты) бот и CRM недоступны.")
    for n in stop_local():
        say("  • " + n)
    time.sleep(3)

    step(4, "Упаковка и отправка данных")
    if run([sys.executable, str(BASE / "deploy" / "migrate_pack.py")]) != 0:
        MARK.unlink(missing_ok=True)
        stop("Данные не упаковались.")
    manifest = {}
    try:
        manifest = json.loads((BASE / "nova-migration.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    mode = manifest.get("tunnel", "none")
    domain = ""
    if mode == "none":
        m = re.match(r"https?://([^/?#]+)", manifest.get("webapp_url", ""))
        domain = m.group(1) if m else ""
        say()
        say(f"Туннель Cloudflare на этом компьютере не найден. Сервер получит собственный сертификат для {domain or 'вашего домена'}.")
        say(f"В DNS домена нужна A-запись {domain or '<домен>'} → {host} (в Cloudflare: серое облако на время выпуска сертификата).")
    say("Отправляю архив на сервер…")
    if run(["scp", *SSH_OPTS, "-i", str(KEY), str(BASE / "nova-migration.zip"), f"root@{host}:/root/nova-migration.zip"]) != 0:
        MARK.unlink(missing_ok=True)
        stop("Архив не отправился (обрыв связи?). Запустите move_to_cloud.bat ещё раз — он продолжит.")

    step(5, "Запуск на сервере")
    rc = ssh(host, f"NOVA_BRANCH='{BRANCH}' bash /root/install_server.sh start {domain}".strip())
    if rc != 0:
        MARK.unlink(missing_ok=True)
        stop("На сервере не запустилось. Пришлите текст выше. Чтобы пока вернуть работу сюда — запустите restart_all.bat.")
    for n in stop_local_tunnel(mode):
        say("  • " + n)
    try:
        (BASE / "nova-migration.zip").unlink()
    except OSError:
        pass

    say()
    say("=" * 64)
    say("  ГОТОВО: Nova Home работает в облаке")
    say("=" * 64)
    pub = check_public(manifest.get("webapp_url", ""))
    say(pub if pub else "Адрес сайта пока не ответил — подождите 1–2 минуты и откройте CRM. Если не откроется, пришлите этот экран.")
    say()
    say("Что дальше:")
    say("  • Проверьте: бот отвечает на /sync, CRM открывается, в CRM слева версия сервера.")
    say("  • Обновления: команда /update боту (сервер сам скачает новую версию), либо cloud_update.bat.")
    say("  • Этот компьютер больше не сервер: его можно выключать. Booking-расширение в Chrome работает как раньше.")
    say("  • Вернуться назад: удалить MOVED_TO_CLOUD.txt и запустить restart_all.bat (см. ПЕРЕЕЗД-В-ОБЛАКО.txt).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
