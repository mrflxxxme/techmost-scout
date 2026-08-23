"""Страж правила 500 строк для каталога `scripts/`.

Правило «файлы до 500 строк» (CLAUDE.md) до сих пор держалось только памятью:
машинная проверка была лишь у модулей пайплайна (`test_pipeline_layout.py`),
и `scripts/history_probe.py` спокойно дорос до 562 строк — долг заметили
только на воротах Э2-Е. Этот тест закрывает пробел: скрипты считаются такой
же частью репозитория, исключений для них не записано.
"""

from __future__ import annotations

from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"

_LINE_LIMIT = 500


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_scripts_stay_within_line_limit():
    """Ни один скрипт не длиннее 500 строк — то же правило, что и для кода продукта."""
    over_limit = [
        f"{path.name}: {_line_count(path)} строк"
        for path in sorted(_SCRIPTS_DIR.glob("*.py"))
        if _line_count(path) > _LINE_LIMIT
    ]
    assert not over_limit, "превышен лимит строк: " + "; ".join(over_limit)


def test_scripts_directory_is_not_empty():
    """Страж считает настоящие файлы: пустой каталог прошёл бы проверку выше молча."""
    assert list(_SCRIPTS_DIR.glob("*.py")), "в scripts/ не найдено ни одного .py — страж считал пустоту"
