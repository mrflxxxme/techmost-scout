"""Статусы запроса: `RequestStatus(StrEnum)` и русские метки (задача И-1).

Проверяем два свойства контракта, закреплённого планом (раздел «Контракты,
закреплённые планом» → «Статусы», РЕШ-143 п. 3, РЕШ-189 п. 11):

1. значения перечисления — ровно семь литералов SPEC, в БД остаются
   английские строки (`RequestStatus` — подкласс `str`, сравнение и запись в
   колонку `String(32)` работают без явного приведения типов);
2. у **каждого** члена перечисления есть непустая русская метка в
   `STATUS_LABELS` — мутационный тест: если метку убрать или обнулить,
   проверка падает.

Отдельно — `Request.status_label`: то самое свойство, через которое шаблоны
`index.html`/`history.html` печатают русский текст вместо `row.status`.
"""

from __future__ import annotations

import pytest

from app.models import STATUS_LABELS, Request, RequestStatus

#: Семь литералов SPEC (docs/SPEC.md, раздел Э3-2) — эталон, с которым
#: сверяются значения перечисления.
SPEC_STATUS_VALUES = (
    "intake",
    "briefing",
    "searching",
    "drafted",
    "validated",
    "exported",
    "failed",
)


def test_request_status_values_match_spec() -> None:
    """Значения перечисления — ровно семь литералов SPEC, без лишних и без пропусков."""
    values = tuple(member.value for member in RequestStatus)
    assert values == SPEC_STATUS_VALUES


def test_request_status_is_str_subclass() -> None:
    """`RequestStatus` — `StrEnum`: член перечисления равен строковому литералу
    без явного приведения типа, ровно так, как он будет сравниваться со
    значением колонки `requests.status` (`String(32)`)."""
    assert isinstance(RequestStatus.FAILED, str)
    assert RequestStatus.FAILED == "failed"


@pytest.mark.parametrize("member", list(RequestStatus))
def test_every_status_has_non_empty_russian_label(member: RequestStatus) -> None:
    """У каждого статуса есть непустая русская метка.

    Мутация: убрать запись из `STATUS_LABELS` или заменить метку на пустую
    строку — этот тест падает на соответствующем члене перечисления.
    """
    assert member in STATUS_LABELS
    label = STATUS_LABELS[member]
    assert isinstance(label, str)
    assert label.strip() != ""


def test_status_labels_has_no_extra_keys() -> None:
    """Словарь меток не содержит ничего, кроме членов `RequestStatus`."""
    assert set(STATUS_LABELS.keys()) == set(RequestStatus)


def test_request_status_label_property_reads_dictionary() -> None:
    """`Request.status_label` возвращает метку из `STATUS_LABELS` по значению колонки."""
    row = Request(raw_text="демо", status=RequestStatus.SEARCHING)
    assert row.status_label == STATUS_LABELS[RequestStatus.SEARCHING]


def test_request_status_label_falls_back_to_raw_value_for_unknown_status() -> None:
    """Незнакомое значение колонки (например, из старой базы) не роняет свойство —
    печатается как есть, а не поднимает исключение на отрисовке страницы."""
    row = Request(raw_text="демо", status="архив")
    assert row.status_label == "архив"
