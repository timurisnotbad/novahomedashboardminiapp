"""Talk to the cloud server from the office computer (after move_to_cloud.bat).

    python deploy\\cloud.py update     new version from GitHub, rebuild, restart
    python deploy\\cloud.py logs       state + live log (Ctrl+C to leave)
    python deploy\\cloud.py status
    python deploy\\cloud.py backup     a copy of the database on the server
"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REMOTE = "bash /opt/novahome/deploy/install_server.sh "


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode not in ("update", "logs", "status", "backup"):
        print("Команды: update | logs | status | backup")
        return 1
    try:
        st = json.loads((BASE / "cloud.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        print("Файла cloud.json нет — сервер в облаке ещё не настроен (move_to_cloud.bat).")
        return 1
    key = st.get("key") or str(Path.home() / ".ssh" / "nova_cloud")
    cmd = REMOTE + ("status; " + REMOTE + "logs" if mode == "logs" else mode)
    print(f"Сервер {st['host']}: {mode}")
    return subprocess.call(["ssh", "-i", key, "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
                            f"root@{st['host']}", cmd])


if __name__ == "__main__":
    sys.exit(main())
