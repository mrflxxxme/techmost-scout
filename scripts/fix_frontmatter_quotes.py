"""Заключить строковые значения шапок в кавычки, если они ломают YAML.

Зачем отдельный скрипт: двоеточие внутри незакавыченного значения делает шапку
невалидным YAML, но глазами это не видно — текст выглядит нормально. Грабля в
проекте случалась трижды: в справочнике господдержки, в шапках команд и в шапках
ролей. Скрипт находит такие значения и приводит их к безопасному виду.

Запуск:
    .venv\\Scripts\\python.exe scripts\\fix_frontmatter_quotes.py            # проверить
    .venv\\Scripts\\python.exe scripts\\fix_frontmatter_quotes.py --write    # исправить
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGETS = (".claude/agents", ".claude/commands")
FRONTMATTER = re.compile(r"^(---\n)(.*?)(\n---\n)", re.DOTALL)
#: Поля, значения которых бывают свободным текстом с двоеточиями
TEXT_FIELDS = ("description", "argument-hint")


def quote_if_needed(head: str) -> tuple[str, list[str]]:
    """Взять в кавычки текстовые поля, если значение содержит двоеточие."""
    fixed_lines: list[str] = []
    changed: list[str] = []
    for line in head.splitlines():
        match = re.match(r"^([A-Za-z-]+):\s*(.+)$", line)
        if match and match.group(1) in TEXT_FIELDS:
            key, value = match.group(1), match.group(2).strip()
            already_quoted = value.startswith(('"', "'")) and value.endswith(('"', "'"))
            if ": " in value and not already_quoted:
                # Внутренние двойные кавычки экранируем, чтобы не порвать значение
                escaped = value.replace('"', '\\"')
                fixed_lines.append(f'{key}: "{escaped}"')
                changed.append(key)
                continue
        fixed_lines.append(line)
    return "\n".join(fixed_lines), changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Починить кавычки в шапках markdown-файлов")
    parser.add_argument("--write", action="store_true", help="записать исправления, иначе только проверить")
    args = parser.parse_args()

    problems = 0
    for target in TARGETS:
        for path in sorted((REPO_ROOT / target).glob("*.md")):
            text = path.read_text(encoding="utf-8")
            match = FRONTMATTER.match(text)
            if match is None:
                print(f"  {path.name}: нет шапки — пропускаю")
                continue

            head = match.group(2)
            try:
                yaml.safe_load(head)
                valid_before = True
            except yaml.YAMLError:
                valid_before = False

            fixed_head, changed = quote_if_needed(head)
            if not changed:
                if not valid_before:
                    print(f"  {path.name}: шапка невалидна, но автоматически не лечится — посмотрите руками")
                    problems += 1
                continue

            try:
                yaml.safe_load(fixed_head)
            except yaml.YAMLError as exc:
                print(f"  {path.name}: после кавычек всё ещё невалидно: {exc}")
                problems += 1
                continue

            problems += 1
            if args.write:
                path.write_text(
                    match.group(1) + fixed_head + match.group(3) + text[match.end() :],
                    encoding="utf-8",
                )
                print(f"  {path.name}: закавычено {', '.join(changed)}")
            else:
                print(f"  {path.name}: требует кавычек в {', '.join(changed)}")

    if problems == 0:
        print("Все шапки валидны.")
        return 0
    if not args.write:
        print(f"\nНайдено файлов к исправлению: {problems}. Запустите с --write.")
        return 1
    print(f"\nИсправлено файлов: {problems}. Проверьте: pytest tests/test_team_docs.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
