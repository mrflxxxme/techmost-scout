"""Автосверка фактов карточек с текстами источников (Э6-6б, РЕШ-252).

Чистые функции кода поверх LLM-стадии `verifier`: стадия ловит смысловые
расхождения, код ловит то, что проверяется без модели, — ссылку и числа. Факт
сверен, когда его ссылка есть среди кандидатов прогона и каждое число
утверждения есть в тексте именно этого источника. Карточка сверена, когда
сверены все её факты, нет `unsourced_claims`, а числа описания встречаются в
текстах её источников.

Текст источника — только то, что поисковый адаптер уже принёс и записал в
`candidates`: название, сниппет, организация, год. Страницы не загружаются
(РЕШ-252), поэтому сверка строгая: число, которого нет в этих полях,
считается несверенным. Урезается объём скана, а не доказательность.

Без сети и без базы: строки таблицы приходят аргументом (инвариант №7).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Protocol

from app.schemas import AnalogCard, Evidence, SolutionCard
from app.search.base import normalize_url


class SourceRow(Protocol):
    """Строка таблицы `candidates`: ровно те поля, из которых собирается текст источника."""

    url: str
    title: str
    snippet: str
    org: str | None
    year: int | None


#: «1 000 000» → «1000000»: группа из 1–3 цифр, затем группы ровно по три. Начало
#: «2020 100» сюда не попадает (первая группа из четырёх цифр) — это два числа.
_THOUSANDS = re.compile(r"(?<!\d)(\d{1,3})((?:\s\d{3})+)(?!\d)")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")
#: Знак «минус» — дефис или U+2212 вплотную к числу и не внутри слова или диапазона:
#: «−40 °C» отрицательно, «10-20», «МФ-300» и «COVID-19» знака не имеют.
_NUMBER = re.compile(r"(?:(?<![\w.-])-)?\d+(?:\.\d+)?")
_MINUS_LIKE = str.maketrans({"\u2212": "-", "\u2010": "-", "\u2011": "-"})


def _glue_thousands(match: re.Match[str]) -> str:
    return match.group(1) + match.group(2).replace(" ", "")


def numbers_in(text: str) -> frozenset[str]:
    """Числа текста в каноническом виде.

    Пробелы (в том числе неразрывные) сводятся к одному, разряды склеиваются,
    десятичная запятая становится точкой. Знак процента, единицы и порядок
    слов значения не имеют: «35%» и «35 %» — одно число. Знак числа имеет:
    «−40» и «40» — разные числа, факт про минус 40 по источнику «до 40» не сверяется.
    """
    flat = " ".join(text.translate(_MINUS_LIKE).split())
    flat = _THOUSANDS.sub(_glue_thousands, flat)
    flat = _DECIMAL_COMMA.sub(".", flat)
    return frozenset(_NUMBER.findall(flat))


def source_texts(rows: Iterable[SourceRow]) -> dict[str, str]:
    """Тексты источников по нормализованной ссылке: название, сниппет, организация, год.

    Одна страница из нескольких источников (дубль в двух проходах поиска)
    склеивается: число из любой её записи считается присутствующим.
    """
    texts: dict[str, str] = {}
    for row in rows:
        parts = [row.title, row.snippet, row.org or "", str(row.year) if row.year else ""]
        # Разделитель не пробел: иначе «12» в конце названия и «345» в начале сниппета склеились бы в 12345.
        text = " | ".join(part for part in parts if part)
        key = normalize_url(row.url)
        texts[key] = f"{texts[key]} | {text}" if key in texts else text
    return texts


def _source_text(url: object, texts: Mapping[str, str]) -> str | None:
    """Текст источника по ссылке или `None`, если такой ссылки среди кандидатов нет."""
    return texts.get(normalize_url(str(url)))


def fact_verified(evidence: Evidence, texts: Mapping[str, str]) -> bool:
    """Факт сверен: ссылка есть среди кандидатов и все числа утверждения есть в её тексте."""
    text = _source_text(evidence.url, texts)
    if text is None:
        return False
    return numbers_in(evidence.claim) <= numbers_in(text)


def _facts_verified(evidence: list[Evidence], texts: Mapping[str, str]) -> bool:
    """Подтверждения непусты и сверены все до единого: один несверенный факт губит карточку."""
    return bool(evidence) and all(fact_verified(item, texts) for item in evidence)


def _numbers_supported(fields: Iterable[str], evidence: list[Evidence], texts: Mapping[str, str]) -> bool:
    """Числа текстовых полей есть в объединённом тексте источников карточки.

    Берутся только ссылки из `evidence`: источник, на который карточка не
    сослалась, подтверждением не служит, даже если он был среди кандидатов.
    """
    source = " ".join(text for item in evidence if (text := _source_text(item.url, texts)) is not None)
    return numbers_in(" ".join(fields)) <= numbers_in(source)


def card_verified(card: SolutionCard, texts: Mapping[str, str]) -> bool:
    """Карточка сверена и допускается в скан.

    Условия: непустой `evidence`, все факты сверены, `unsourced_claims` пуст,
    числа `title`, `essence` и `applicability` есть в текстах источников карточки.
    `developer_org` и `gov_support.instrument` не сверяются: организация берётся
    из источника как есть, инструмент — из справочника господдержки.
    """
    if card.unsourced_claims or not _facts_verified(card.evidence, texts):
        return False
    return _numbers_supported([card.title, card.essence, card.applicability], card.evidence, texts)


def analog_verified(card: AnalogCard, texts: Mapping[str, str]) -> bool:
    """Аналог сверен: то же, что у карточки, плюс ссылка, подтверждающая страну, среди кандидатов.

    Числа проверяются во всех текстовых полях, которые скан печатает:
    `title`, `essence`, `business_model`, `traction`, `transferability_ru`.
    """
    if _source_text(card.country_evidence_url, texts) is None or not _facts_verified(card.evidence, texts):
        return False
    fields = [card.title, card.essence, card.business_model, card.traction, card.transferability_ru]
    return _numbers_supported(fields, card.evidence, texts)
