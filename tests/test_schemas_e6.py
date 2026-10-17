"""Схемные контракты этапа 6 «Рынок» (Э6-1): `AnalogCard`, `Candidate.country`,
`SourceType.ANALOG`, `Report.analog_cards`, `Brief.solution_kind`.

Поведения аналогов задача не вводит — проверяется только форма данных.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas import (
    MARKET_KINDS,
    AnalogCard,
    Brief,
    Candidate,
    Report,
    ReportSummary,
    SourceType,
)

_BAD_COUNTRIES = ["us", "USA", "", "U1", "У", "US "]


def _analog(**overrides) -> dict:
    data = {
        "title": "Аналог",
        "essence": "Суть",
        "country": "US",
        "country_evidence_url": "https://example.com/about",
        "business_model": "подписка",
        "transferability_ru": "переносится",
        "what_to_do_in_ru": "связаться",
    }
    return {**data, **overrides}


def _candidate(**overrides) -> dict:
    data = {"source_type": "analog", "title": "T", "url": "https://example.com/x", "provider": "wikidata"}
    return {**data, **overrides}


def test_solution_kind_не_попадает_в_дамп_брифа(brief: Brief):
    """Бриф уходит в промпты дампом: вид решения туда не попадает, кэш префикса цел."""
    marked = brief.model_copy(update={"solution_kind": "digital"})

    assert "solution_kind" not in marked.model_dump_json()
    assert marked.model_dump_json(indent=2) == brief.model_dump_json(indent=2)


def test_solution_kind_по_умолчанию_research_и_только_четыре_вида(brief: Brief):
    assert brief.solution_kind == "research"
    for kind in MARKET_KINDS:
        assert Brief(**brief.model_dump(), solution_kind=kind).solution_kind == kind
    with pytest.raises(ValidationError):
        Brief(**brief.model_dump(), solution_kind="unknown")


def test_тип_источника_analog_есть():
    assert SourceType.ANALOG == "analog"


@pytest.mark.parametrize("code", _BAD_COUNTRIES)
def test_candidate_country_отвергает_не_iso_alpha2(code: str):
    with pytest.raises(ValidationError):
        Candidate(**_candidate(country=code))


def test_candidate_country_по_умолчанию_пусто_и_принимает_us():
    assert Candidate(**_candidate()).country is None
    assert Candidate(**_candidate(country="US")).country == "US"


@pytest.mark.parametrize("code", _BAD_COUNTRIES)
def test_analog_card_country_отвергает_не_iso_alpha2(code: str):
    with pytest.raises(ValidationError):
        AnalogCard(**_analog(country=code))


def test_analog_card_минимальная_форма():
    card = AnalogCard(**_analog())

    assert card.country == "US"
    assert card.traction == ""
    assert card.evidence == []
    assert card.candidate_urls == []


def test_старый_json_отчёта_без_analog_cards_читается_с_пустым_списком(brief: Brief):
    report = Report(
        request_id=1,
        brief=brief,
        cards=[],
        summary=ReportSummary(recommendation="r"),
        generated_at=datetime(2026, 10, 17, tzinfo=UTC),
    )
    old = report.model_dump(mode="json")
    del old["analog_cards"]

    loaded = Report.model_validate(old)

    assert loaded.analog_cards == []
