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


def main() -> int:
    example = EXAMPLE.read_text(encoding="utf-8-sig")
    if not ENV.exists():
        ENV.write_text(example, encoding="utf-8")
        print("Создан .env из .env.example — впишите в него токены (см. ПРОЧТИ-МЕНЯ.txt)")
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
        return 0
    with ENV.open("a", encoding="utf-8") as fh:
        fh.write("\n\n# ---- Добавлено обновлением (новые настройки, значения по умолчанию) ----\n")
        fh.write("\n".join(missing_block) + "\n")
    added = [l.split("=", 1)[0] for l in missing_block if l and not l.lstrip().startswith("#") and "=" in l]
    print(f"В .env добавлены новые настройки ({len(added)}): " + ", ".join(added))
    print("Пустые значения = функция выключена. Заполните то, что нужно, и перезапустите restart_all.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
