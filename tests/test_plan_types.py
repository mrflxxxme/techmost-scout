"""Структурная сверка: типы промпта планировщика = рабочие адаптеры поиска.

Механизм DLG-1 из прошлого прогона: правило 1 `planner.md` разрешало типы
`publication`, `patent`, `grant`, `org`, а рабочими (implemented = True)
адаптерами были только `showcase`, `crossref`, `cyberleninka`, `arxiv`,
`semanticscholar`, `yandex_search`, `google_search` — половина плана уходила
в никуда, а веб-поиск и витрина не получали ни одного запроса. РЕШ-39: состав
типов запроса = состав рабочих адаптеров.

Тест держит соответствие в обе стороны — расхождение в любую сторону снова
воспроизводит DLG-1 — и проверяет, что промпт не объявляет типов за пределами
`SourceType`. Разбор промпта идёт по строке правила 1, а не по всему тексту:
упоминания старых типов в пояснениях («гранты не пропадают как задача
поиска») не должны считаться объявлением разрешённого значения.
"""

from __future__ import annotations

import re

from app.config import get_settings
from app.pipeline.stages import load_prompt
from app.schemas import SourceType
from app.search import ADAPTERS

#: Строка правила 1 planner.md — единственное место, где типы объявлены как
#: разрешённые значения `source_type`.
_RULE_1_LINE = re.compile(r"^1\.\s*Типы источников.*$", re.MULTILINE)
_BACKTICKED = re.compile(r"`(\w+)`")
_CHANGELOG_ENTRY_VERSION = re.compile(r"^##\s*([\d.]+)\s", re.MULTILINE)
_FILE_VERSION_LABEL = re.compile(r"<!--\s*версия\s*([\d.]+)\s*-->")


def _planner_allowed_types() -> set[str]:
    """Типы, разрешённые правилом 1 planner.md, по тексту самой строки правила."""
    text = load_prompt("planner")
    match = _RULE_1_LINE.search(text)
    assert match, "planner.md: не найдена строка правила 1 с типами источников"
    return set(_BACKTICKED.findall(match.group(0)))


def _implemented_adapter_source_types() -> set[str]:
    """Типы источников, которые реально обслуживаются рабочими адаптерами."""
    types: set[str] = set()
    for adapter_cls in ADAPTERS.values():
        if not getattr(adapter_cls, "implemented", True):
            continue
        types.update(source_type.value for source_type in adapter_cls.source_types)
    return types


def test_каждый_тип_промпта_обслуживается_рабочим_адаптером():
    """Правило 1 не должно разрешать тип, для которого нет ни одного рабочего
    адаптера, — иначе формулировка уходит в никуда (половина DLG-1)."""
    prompt_types = _planner_allowed_types()
    implemented_types = _implemented_adapter_source_types()
    unreachable = prompt_types - implemented_types
    assert not unreachable, f"planner.md разрешает типы без рабочего адаптера: {sorted(unreachable)}"


def test_каждый_рабочий_адаптер_достижим_типом_из_промпта():
    """Обратное направление: рабочий адаптер, который планировщик никогда не
    запросит, простаивает весь прогон (вторая половина DLG-1)."""
    prompt_types = _planner_allowed_types()
    for name, adapter_cls in ADAPTERS.items():
        if not getattr(adapter_cls, "implemented", True):
            continue
        adapter_types = {source_type.value for source_type in adapter_cls.source_types}
        assert adapter_types & prompt_types, (
            f"адаптер {name} ({sorted(adapter_types)}) недостижим ни одним типом промпта "
            f"planner.md ({sorted(prompt_types)})"
        )


def test_типы_промпта_подмножество_source_type():
    """Правило 1 не должно объявлять значение, которого нет в SourceType вовсе."""
    prompt_types = _planner_allowed_types()
    known_types = {member.value for member in SourceType}
    unknown = prompt_types - known_types
    assert not unknown, f"planner.md разрешает неизвестные типы: {sorted(unknown)}"


def test_патентный_запрос_один_и_на_русском():
    """Правило 2 обязано задавать по `patent` ровно одну русскую формулировку.

    Тип разрешён ради рабочего патентного источника, но цена прогона не должна
    вырасти: одна формулировка — один дополнительный запрос. Английская
    формулировка в патентном поиске по РФ бесполезна — база ищет по русскому
    тексту, поэтому язык задан жёстко, а не оставлен на усмотрение модели.
    """
    text = load_prompt("planner")
    rule_2 = re.search(r"^2\.\s.*?(?=^\d+\.\s)", text, re.MULTILINE | re.DOTALL)
    assert rule_2, "planner.md: не найдено правило 2 про число формулировок"
    body = rule_2.group(0)
    assert "`patent`" in body, "planner.md: правило 2 не задаёт число формулировок на `patent`"
    assert 'lang: "ru"' in body, "planner.md: правило 2 не требует русского языка для `patent`"
    assert "от 6 до 9" in body, "planner.md: правило 2 не обновило общий объём плана под патентный запрос"


def test_патент_достижим_обоими_патентными_адаптерами():
    """Разрешён ТИП, а не источник: под `patent` обязаны подходить оба патентных
    адаптера — `rospatent` и `patents_view` (Т-9), независимо от того, какой из
    них уже реализован."""
    prompt_types = _planner_allowed_types()
    for name in ("rospatent", "patents_view"):
        adapter_cls = ADAPTERS[name]
        adapter_types = {source_type.value for source_type in adapter_cls.source_types}
        assert adapter_types & prompt_types, (
            f"патентный адаптер {name} ({sorted(adapter_types)}) не покрыт типами промпта {sorted(prompt_types)}"
        )


def test_версия_planner_совпадает_с_changelog():
    """Метка версии planner.md обязана совпадать с версией самой свежей записи
    CHANGELOG.md, где этот файл упомянут (та же проверка, что для card_author и
    verifier в tests/test_prompts.py, здесь — для planner.md отдельно)."""
    text = load_prompt("planner")
    label_match = _FILE_VERSION_LABEL.search(text)
    assert label_match, "planner.md: нет метки версии <!-- версия X.Y -->"
    file_version = label_match.group(1)

    changelog = (get_settings().prompts_dir / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = re.split(r"\n(?=## )", changelog)
    mentioning = [entry for entry in entries if "`planner.md`" in entry]
    assert mentioning, "в CHANGELOG.md нет ни одной записи про planner.md"
    latest_entry = mentioning[0]  # записи идут от новых к старым
    version_match = _CHANGELOG_ENTRY_VERSION.search(latest_entry)
    assert version_match, "запись CHANGELOG.md про planner.md без номера версии в заголовке"
    assert version_match.group(1) == file_version, (
        f"planner.md: версия в метке файла {file_version} не совпадает с версией последней записи "
        f"CHANGELOG.md {version_match.group(1)}"
    )
