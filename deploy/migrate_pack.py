"""Pack everything the cloud server needs from the office computer into one
file: nova-migration.zip (next to bot.py).

  env                 the .env with all keys
  nova_dashboard.db   a consistent copy of the database (safe while Nova runs)
  data/…              secrets.json, chat media, Telegram account session
  wa-auth/…           the WhatsApp QR session (if the QR bridge is used)
  rc_cookies.json     RealtyCalendar cookies, when .env points to a file
  cloudflared/…       the Cloudflare Tunnel identity, so the address
                      app.novahomedashboard.com keeps working without DNS changes
  manifest.json       what is inside

Only the standard library; run by move_to_cloud.bat or by hand:
    python deploy\\migrate_pack.py
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "nova-migration.zip"
NO_COMPRESS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".ogg", ".oga", ".opus", ".mp3", ".pdf", ".zip", ".m4a", ".webm", ".gif"}


def read_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] in "\"'" and v.endswith(v[0]):
            v = v[1:-1]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        out[k.strip()] = v
    return out


def db_snapshot(src: Path, dst: Path) -> int:
    """SQLite online backup: a clean single file even while the server writes."""
    s = sqlite3.connect(str(src), timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        n = d.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    finally:
        d.close()
        s.close()
    return n


def add_tree(z: zipfile.ZipFile, root: Path, arc: str, skip=()) -> tuple[int, int]:
    files = size = 0
    if not root.is_dir():
        return 0, 0
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        try:
            comp = zipfile.ZIP_STORED if p.suffix.lower() in NO_COMPRESS else zipfile.ZIP_DEFLATED
            z.write(p, f"{arc}/{rel}", compress_type=comp)
            files += 1
            size += p.stat().st_size
        except OSError as exc:  # a file locked by a running program
            print(f"    пропущен {rel}: {exc}")
    return files, size


def cloudflared_identity() -> dict:
    """{'token': str} for a dashboard-managed tunnel installed as a Windows
    service, or {'config': Path, 'creds': Path, 'tunnel': id, 'hosts': [...]}
    for a locally managed one (config.yml + <id>.json). {} when none."""
    if os.name == "nt":
        try:
            r = subprocess.run(["reg", "query", r"HKLM\SYSTEM\CurrentControlSet\Services\Cloudflared", "/v", "ImagePath"],
                               capture_output=True, text=True, timeout=15)
            m = re.search(r"--token\s+([A-Za-z0-9_\-=.]{40,})", r.stdout or "")
            if m:
                return {"token": m.group(1)}
        except Exception:  # noqa: BLE001
            pass
        try:  # a console window: cloudflared.exe tunnel run --token …
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'cloudflared*' } | ForEach-Object { $_.CommandLine }"],
                               capture_output=True, text=True, timeout=25)
            m = re.search(r"--token\s+([A-Za-z0-9_\-=.]{40,})", r.stdout or "")
            if m:
                return {"token": m.group(1)}
        except Exception:  # noqa: BLE001
            pass
    for d in (Path.home() / ".cloudflared", Path(os.environ.get("ProgramData", "C:/ProgramData")) / "cloudflared", Path("/etc/cloudflared")):
        for name in ("config.yml", "config.yaml"):
            cfg = d / name
            if not cfg.exists():
                continue
            text = cfg.read_text(encoding="utf-8", errors="replace")
            tm = re.search(r"(?m)^\s*tunnel:\s*([0-9a-fA-F-]{36}|\S+)", text)
            cm = re.search(r"(?m)^\s*credentials-file:\s*(.+?)\s*$", text)
            creds = Path(cm.group(1).strip().strip("\"'")) if cm else None
            if (creds is None or not creds.exists()) and tm:
                cand = d / f"{tm.group(1)}.json"
                creds = cand if cand.exists() else None
            if creds is None:
                js = sorted(d.glob("*.json"))
                creds = js[0] if js else None
            if creds and creds.exists():
                tid = tm.group(1) if tm else creds.stem
                try:
                    tid = json.loads(creds.read_text(encoding="utf-8")).get("TunnelID") or tid
                except Exception:  # noqa: BLE001
                    pass
                hosts = re.findall(r"(?m)^\s*-?\s*hostname:\s*(\S+)", text)
                return {"config": cfg, "creds": creds, "tunnel": tid, "hosts": hosts}
    return {}


def main() -> int:
    env_path = BASE / ".env"
    if not env_path.exists():
        print("!!! Рядом с bot.py нет файла .env — это не рабочая папка Nova Home.")
        return 1
    env = read_env(env_path)
    db = Path(env.get("DB_PATH") or BASE / "nova_dashboard.db")
    if not db.is_absolute():
        db = BASE / db
    manifest = {"created": datetime.now().isoformat(timespec="seconds"), "computer": os.environ.get("COMPUTERNAME", ""),
                "webapp_url": env.get("WEBAPP_URL", ""), "parts": {}}
    try:
        cfg = (BASE / "backend" / "config.py").read_text(encoding="utf-8")
        manifest["version"] = re.search(r'APP_VERSION = "(\d+)"', cfg).group(1)
    except Exception:  # noqa: BLE001
        manifest["version"] = "?"

    tmp = Path(tempfile.mkdtemp(prefix="nova-pack-"))
    if OUT.exists():
        OUT.unlink()
    print(f"Собираю {OUT.name} …")
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.write(env_path, "env")
        manifest["parts"]["env"] = len(env)
        print(f"  .env: {len(env)} настроек")

        if db.exists():
            snap = tmp / "nova_dashboard.db"
            tables = db_snapshot(db, snap)
            z.write(snap, "nova_dashboard.db")
            manifest["parts"]["db"] = {"tables": tables, "mb": round(snap.stat().st_size / 1e6, 1)}
            print(f"  база: {tables} таблиц, {snap.stat().st_size / 1e6:.1f} МБ")
        else:
            print(f"  !!! база не найдена: {db}")

        data = BASE / "data"
        media = Path(env.get("INBOX_MEDIA_DIR") or data / "inbox_media")
        n, size = add_tree(z, data, "data")
        if media.exists() and data not in media.parents and media != data / "inbox_media":
            n2, s2 = add_tree(z, media, "data/inbox_media")
            n, size = n + n2, size + s2
        manifest["parts"]["data"] = {"files": n, "mb": round(size / 1e6, 1)}
        print(f"  data (ключи, медиа чатов, сессия Telegram): {n} файлов, {size / 1e6:.1f} МБ")

        n, size = add_tree(z, BASE / "wa-bridge" / "auth", "wa-auth")
        manifest["parts"]["wa_auth"] = n
        if n:
            print(f"  сессия WhatsApp (QR): {n} файлов")

        rc_file = env.get("RC_COOKIES_FILE", "")
        if rc_file and Path(rc_file).exists():
            z.write(rc_file, "rc_cookies.json")
            manifest["parts"]["rc_cookies"] = True
            print("  cookies RealtyCalendar: файл добавлен")

        cf = cloudflared_identity()
        if cf.get("token"):
            z.writestr("cloudflared/token.txt", cf["token"])
            manifest["tunnel"] = "token"
            print("  туннель Cloudflare: найден (служба Windows) — адрес сайта останется прежним")
        elif cf.get("creds"):
            tid = cf["tunnel"]
            hosts = cf.get("hosts") or []
            z.write(cf["creds"], f"cloudflared/{tid}.json")
            lines = [f"tunnel: {tid}", f"credentials-file: /etc/cloudflared/{tid}.json", "ingress:"]
            for h in hosts:
                lines += [f"  - hostname: {h}", "    service: http://localhost:8000"]
            lines.append("  - service: http://localhost:8000" if not hosts else "  - service: http_status:404")
            z.writestr("cloudflared/config.yml", "\n".join(lines) + "\n")
            manifest["tunnel"] = "file"
            manifest["tunnel_hosts"] = hosts
            print(f"  туннель Cloudflare: найден (config.yml{', ' + ', '.join(hosts) if hosts else ''}) — адрес останется прежним")
        else:
            manifest["tunnel"] = "none"
            print("  туннель Cloudflare не найден — понадобится DNS-запись на новый сервер")

        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    try:
        (tmp / "nova_dashboard.db").unlink(missing_ok=True)
        tmp.rmdir()
    except OSError:
        pass
    print(f"Готово: {OUT}  ({OUT.stat().st_size / 1e6:.1f} МБ)")
    (BASE / "nova-migration.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
