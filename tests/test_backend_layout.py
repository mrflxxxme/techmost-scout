"""Страж правила 500 строк для всего `backend/app` (РЕШ-174, задача З-5).

Правило CLAUDE.md «файлы до 500 строк» до сих пор проверялось точечно —
только для каталога `app.pipeline` (`test_pipeline_layout.py`) и для
`scripts/` (`test_scripts_layout.py`). Остальной `backend/app` жил на памяти,
и аудит нашёл три файла, давно перешагнувших лимит и никем не замеченных.
Этот тест закрывает пробел: проверяется каждый `.py`-файл каталога
рекурсивно, с явным списком исключений вместо тихого игнорирования.

Список исключений может только сокращаться (дробление файла — долг следующей
волны, а не повод расширить список, П-1 аудита): добавить сюда новый файл
можно лишь вместе с записью в `PLAN.md`, объясняющей, почему он не дробится.
"""

from __future__ import annotations

from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[1] / "backend" / "app"

_LINE_LIMIT = 500

#: Файлы старше лимита на момент задачи З-5 (РЕШ-174) — дробление отложено
#: сознательно (границы задачи, `.planning/PLAN.md`). Число рядом с именем —
#: фактический размер на момент фиксации исключения, для контроля роста.
_EXCEPTIONS: dict[str, int] = {
    "cli.py": 674,
    "search/openalex.py": 593,
    # 544 → 558: задача З-4 той же волны добавила тип grant в контур
    # (source_types + сужение формулировки) — рост сознательный, влит
    # оркестратором вместе со слиянием З-4; дробление файла — долг ворот.
    "search/yandex_search.py": 558,
}


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_backend_app_stays_within_line_limit():
    """Ни один файл `backend/app` не длиннее 500 строк, кроме именованных
    исключений. Мутация лимита до 400 должна назвать ровно три файла."""
    over_limit = []
    for path in sorted(_APP_DIR.rglob("*.py")):
        relative = path.relative_to(_APP_DIR).as_posix()
        if relative in _EXCEPTIONS:
            continue
        count = _line_count(path)
        if count > _LINE_LIMIT:
            over_limit.append(f"{relative}: {count} строк")
    assert not over_limit, "превышен лимит строк: " + "; ".join(over_limit)


def test_exceptions_do_not_grow_unnoticed():
    """Исключения фиксируют размер на момент задачи — рост сверх него незаметно
    для ревью означает, что файл дробят в неверную сторону."""
    grown = []
    for relative, recorded in _EXCEPTIONS.items():
        path = _APP_DIR / relative
        count = _line_count(path)
        if count > recorded:
            grown.append(f"{relative}: было {recorded}, стало {count}")
    assert not grown, "исключения выросли без правки списка: " + "; ".join(grown)


def test_backend_app_directory_is_not_empty():
    """Страж считает настоящие файлы: пустой каталог прошёл бы проверку выше молча."""
    assert list(_APP_DIR.rglob("*.py")), "в backend/app не найдено ни одного .py — страж считал пустоту"
