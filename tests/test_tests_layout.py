"""Страж роста тестовых файлов (Д-7 волны Э2-З, уточнение правила 500 CLAUDE.md).

Правило «файлы до 500 строк» из CLAUDE.md — про код продукта: за ним стоят
машинные стражи `backend/app` (`test_backend_layout.py`) и `scripts/`
(`test_scripts_layout.py`). Тестовые файлы под тот же жёсткий предел не
поставлены сознательно — тест на сквозной сценарий или на матрицу подмен
законно длиннее модуля, который он проверяет. Но «без предела» — не значит
«без присмотра»: этот страж работает по образцу `_EXCEPTIONS` из
`test_backend_layout.py` ДО того, как список исключений опустел (Д-5 волны
Э2-З) — уже выросшие файлы зафиксированы парой имя/размер, рост сверх
зафиксированного падает, а новый тестовый файл сверх лимита без записи в
список — тоже красная строка. Так рост виден на ревью, а не копится молча.

Список пополняется вместе с задачей, которая раздула файл сверх
зафиксированного размера — не тихой правкой числа в этом файле.
"""

from __future__ import annotations

from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

_LINE_LIMIT = 500

#: Файлы, уже выросшие сверх 500 строк на момент задачи Д-7 (волна Э2-З) —
#: размер зафиксирован по факту, не одобрение дальнейшего роста. Дробить эти
#: файлы никто пока не поручал; решение — за следующей задачей.
_EXCEPTIONS: dict[str, int] = {
    "test_cli_run.py": 878,
    "test_contest_section_flow.py": 542,
    "test_history_reuse.py": 797,
    "test_openalex.py": 1283,
    "test_persistence.py": 1164,
    "test_pipeline_resilience.py": 991,
    # 784 → 791: задача Д-6 той же волны провела судью дублей через тест
    # второго прохода (5 → 6 вызовов модели) — рост сознательный, влит
    # оркестратором при слиянии Д-6 (тот же класс стыка, что у стража
    # backend в волне Э2-Ж).
    "test_pipeline_second_pass.py": 791,
    "test_run_accounting.py": 653,
    "test_search_yandex.py": 607,
    "test_stages.py": 1702,
}

#: Свой собственный файл не считается: это страж, а не то, что он охраняет.
_SELF = "test_tests_layout.py"


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_new_test_files_stay_within_line_limit():
    """Тестовый файл, не входящий в зафиксированный список исключений, не
    длиннее 500 строк. Мутация: раздуть новый файл сверх предела — страж
    называет его."""
    over_limit = []
    for path in sorted(_TESTS_DIR.glob("*.py")):
        if path.name in _EXCEPTIONS or path.name == _SELF:
            continue
        count = _line_count(path)
        if count > _LINE_LIMIT:
            over_limit.append(f"{path.name}: {count} строк")
    assert not over_limit, "новый тестовый файл вырос сверх 500 строк без записи в исключения: " + "; ".join(over_limit)


def test_exceptions_do_not_grow_unnoticed():
    """Зафиксированные файлы не растут молча: рост сверх записанного размера —
    красная строка, которую видно на ревью. Мутация: раздуть один из файлов
    списка — страж называет именно его."""
    grown = []
    for name, recorded in _EXCEPTIONS.items():
        path = _TESTS_DIR / name
        count = _line_count(path)
        if count > recorded:
            grown.append(f"{name}: было {recorded}, стало {count}")
    assert not grown, "исключения выросли без правки списка: " + "; ".join(grown)


def test_tests_directory_is_not_empty():
    """Страж считает настоящие файлы: пустой каталог прошёл бы проверку выше молча."""
    assert list(_TESTS_DIR.glob("*.py")), "в tests/ не найдено ни одного .py — страж считал пустоту"
