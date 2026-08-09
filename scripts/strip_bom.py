"""Убрать метку порядка байтов (BOM) из markdown- и json-файлов проекта.

Зачем: PowerShell 5.1 при записи в кодировке utf8 добавляет BOM. Три байта
перед первым символом делают шапку `---` невидимой для разбора: файл выглядит
правильным, а парсер сообщает «нет шапки». Дефект того же класса, что двоеточие
в незакавыченном значении — глазами не виден.

Запуск:
    .venv\\Scripts\\python.exe scripts\\strip_bom.py            # проверить
    .venv\\Scripts\\python.exe scripts\\strip_bom.py --write    # убрать
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATTERNS = ("*.md", "*.json", "*.yaml", "*.yml", "*.py")
SKIP_DIRS = {".git", ".venv", "node_modules", "out", "__pycache__", ".ruff_cache", ".pytest_cache"}
BOM = b"\xef\xbb\xbf"


def iter_files() -> list[Path]:
    found: list[Path] = []
    for pattern in PATTERNS:
        for path in REPO_ROOT.rglob(pattern):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            found.append(path)
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser(description="Убрать BOM из текстовых файлов проекта")
    parser.add_argument("--write", action="store_true", help="записать исправления")
    args = parser.parse_args()

    affected: list[Path] = []
    for path in iter_files():
        data = path.read_bytes()
        if not data.startswith(BOM):
            continue
        affected.append(path)
        if args.write:
            path.write_bytes(data[len(BOM) :])

    if not affected:
        print("BOM не найден ни в одном файле.")
        return 0

    for path in affected:
        print(f"  {path.relative_to(REPO_ROOT).as_posix()}")
    if args.write:
        print(f"\nBOM убран из файлов: {len(affected)}")
        return 0
    print(f"\nФайлов с BOM: {len(affected)}. Запустите с --write.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
