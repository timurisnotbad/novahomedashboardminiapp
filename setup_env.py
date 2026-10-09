"""Bring .env up to date without touching what is already filled in.

Creates .env from .env.example on a fresh install; on an update appends the
keys the new version introduced (with their comments) to the end of the
existing .env, so nothing the owner configured is lost and nothing has to be
copied by hand. Run by install.bat.
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
EXAMPLE = BASE / ".env.example"
ENV = BASE / ".env"


def keys_of(text: str) -> set[str]:
    out = set()
    for line in text.replace("﻿", "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            out.add(line.split("=", 1)[0].strip())
    return out


ADD = BASE / "env.add"


def apply_add() -> None:
    """env.add (KEY=value lines, shipped inside a personal update archive):
    fills keys that are missing or empty in .env, never overwrites a value
    the owner already set. Removed after it is applied."""
    if not ADD.exists():
        return
    values = {}
    for line in ADD.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    if not values:
        return
    lines = ENV.read_text(encoding="utf-8-sig").splitlines() if ENV.exists() else []
    done = set()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, _, rest = s.partition("=")
        k = k.strip()
        if k in values:
            cur = re.split(r"\s+#", rest, maxsplit=1)[0].strip().strip("\"'")
            if not cur:
                lines[i] = f"{k}={values[k]}"
                print(f"  {k}: заполнено из env.add")
            else:
                print(f"  {k}: уже задано, оставлено как есть")
            done.add(k)
    for k, v in values.items():
        if k not in done:
            lines.append(f"{k}={v}")
            print(f"  {k}: добавлено из env.add")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ADD.unlink()


def main() -> int:
    example = EXAMPLE.read_text(encoding="utf-8-sig")
    if not ENV.exists():
        ENV.write_text(example, encoding="utf-8")
        print("Создан .env из .env.example — впишите в него токены (см. ПРОЧТИ-МЕНЯ.txt)")
        apply_add()
        return 0
    current = ENV.read_text(encoding="utf-8-sig")
    have = keys_of(current)
    missing_block: list[str] = []
    pending_comments: list[str] = []
    for line in example.splitlines():
        s = line.strip()
        if s.startswith("# ----"):
            pending_comments = [line]  # section header: kept only if a key from it is added
            continue
        if s.startswith("#") or not s:
            if s:
                pending_comments.append(line)
            continue
        key = s.split("=", 1)[0].strip()
        if key in have:
            pending_comments = []
            continue
        missing_block.extend(pending_comments)
        pending_comments = []
        missing_block.append(line)
    if not missing_block:
        print(".env уже содержит все настройки этой версии")
        apply_add()
        return 0
    with ENV.open("a", encoding="utf-8") as fh:
        fh.write("\n\n# ---- Добавлено обновлением (новые настройки, значения по умолчанию) ----\n")
        fh.write("\n".join(missing_block) + "\n")
    added = [l.split("=", 1)[0] for l in missing_block if l and not l.lstrip().startswith("#") and "=" in l]
    print(f"В .env добавлены новые настройки ({len(added)}): " + ", ".join(added))
    print("Пустые значения = функция выключена. Заполните то, что нужно, и перезапустите restart_all.bat")
    apply_add()
    return 0


if __name__ == "__main__":
    sys.exit(main())
