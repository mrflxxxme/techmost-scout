"""Перенос ключей из .env другого проекта в .env этого репозитория.

Зачем на Python, а не на PowerShell: на рабочей машине политика запуска
скриптов требует цифровой подписи и не обходится флагом -ExecutionPolicy,
поэтому .ps1 не запускается. Python из виртуального окружения работает всегда.

Скрипт НЕ печатает значения ключей — только имена и статус, чтобы секреты не
попали в историю терминала.

Запуск:
    .venv\\Scripts\\python.exe scripts\\import_keys.py
    .venv\\Scripts\\python.exe scripts\\import_keys.py --source "путь\\к\\.env" --force
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SOURCE = Path(r"C:\Users\KUklonskiy\Obsidian\TGKB\Projects\TEAMLY_RU\backend\.env")

#: Что переносим. Остальное в исходном файле относится к другому проекту.
WANTED = (
    "DEEPSEEK_API_KEY",
    "GIGACHAT_AUTH_KEY",
    "YANDEX_API_KEY",
    "YANDEX_IAM_TOKEN",
    "YANDEX_CATALOG_ID",
    "YANDEX_SEARCH_API_KEY",
    "FX_RATE_USD_TO_RUB",
)

LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def read_env(path: Path) -> dict[str, str]:
    """Разобрать .env. Заглушки TBD_* считаем отсутствием значения."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.lstrip().startswith("#"):
            continue
        match = LINE.match(raw)
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip().strip('"').strip("'")
        if value and not value.startswith("TBD_"):
            values[name] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Перенос ключей из .env другого проекта")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="файл .env источника")
    parser.add_argument("--force", action="store_true", help="перезаписывать уже заполненные значения")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"Не найден файл-источник: {args.source}")
        print("Укажите путь параметром --source")
        return 1

    target = REPO_ROOT / ".env"
    example = REPO_ROOT / ".env.example"
    if not target.exists():
        if not example.exists():
            print("Нет ни .env, ни .env.example — репозиторий неполный.")
            return 1
        shutil.copy(example, target)
        print("Создан .env из .env.example")

    source_values = read_env(args.source)
    lines = target.read_text(encoding="utf-8").splitlines()

    imported: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []

    for name in WANTED:
        if name not in source_values:
            missing.append(name)
            continue

        pattern = re.compile(rf"^\s*{re.escape(name)}\s*=")
        index = next((i for i, line in enumerate(lines) if pattern.match(line)), None)

        if index is None:
            lines.append(f"{name}={source_values[name]}")
        else:
            current = lines[index].split("=", 1)[1].strip()
            if current and not args.force:
                skipped.append(name)
                continue
            lines[index] = f"{name}={source_values[name]}"
        imported.append(name)

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\nПеренос завершён.")
    if imported:
        print("  Перенесено:      " + ", ".join(imported))
    if skipped:
        print("  Уже заполнено:   " + ", ".join(skipped) + "   (перезаписать: --force)")
    if missing:
        print("  Нет в источнике: " + ", ".join(missing))
    print("\nЗначения не выводятся намеренно. Проверить работоспособность:")
    print("  .venv\\Scripts\\scout.exe doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
