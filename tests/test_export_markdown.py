"""Проверки экспортёра markdown: стадия «не определена», сверка с брифом,
ссылка на полный текст. Задача Ф-4 — экспортёр несёт машинную проверку
инварианта №1: карточка без evidence не имеет права заявлять зрелость."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.export.markdown import render_markdown
from app.schemas import (
    Evidence,
    MaturityStage,
    Report,
    ReportSummary,
    RequirementCheck,
    SolutionCard,
)


def _make_card(**overrides) -> SolutionCard:
    """Карточка с минимально достаточным набором полей — переопределяемая точечно."""
    fields = {
        "title": "Регенерация катализатора добавками молибдена",
        "essence": "Продление срока службы катализатора за счёт присадки.",
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "next_step": "Запросить образец у разработчика.",
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _make_report(cards: list[SolutionCard], **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": cards,
        "summary": ReportSummary(recommendation="Начать с варианта №1."),
        "generated_at": datetime(2026, 8, 12, 10, 0),
    }
    fields.update(overrides)
    return Report(**fields)


def test_card_without_evidence_hides_maturity_and_trl(brief):
    """Машинная проверка инварианта №1: без evidence нет ни строки о стадии, ни TRL."""
    card = _make_card(maturity_stage=MaturityStage.LAB, trl_estimate=5, evidence=[])
    report = _make_report([card], brief=brief)

    text = render_markdown(report)

    assert "Стадия зрелости" not in text
    assert "TRL" not in text
    assert "Подтверждений не приложено" in text


def test_unknown_stage_prints_stage_not_defined(brief):
    """Стадия unknown с непустым evidence печатается словами, а не значением перечисления."""
    card = _make_card(
        maturity_stage=MaturityStage.UNKNOWN,
        evidence=[Evidence(claim="Работает в лаборатории", url="https://example.org/a")],
    )
    report = _make_report([card], brief=brief)

    text = render_markdown(report)

    assert "стадия не определена" in text
    assert "**Стадия зрелости:** unknown" not in text


def test_maturity_still_printed_when_evidence_present(brief):
    """Регрессия: карточка с подтверждениями по-прежнему показывает стадию и TRL."""
    card = _make_card(
        maturity_stage=MaturityStage.PILOT,
        trl_estimate=6,
        evidence=[Evidence(claim="Пилот на объекте", url="https://example.org/a")],
    )
    report = _make_report([card], brief=brief)

    text = render_markdown(report)

    assert "**Стадия зрелости:** прошла пилот на объекте (TRL ≈ 6)" in text


def test_requirement_checks_printed_one_per_line(brief):
    """По каждому пункту requirement_checks — одна строка: подтверждено или нет."""
    card = _make_card(
        requirement_checks=[
            RequirementCheck(
                requirement="работа при 340 °C",
                status="confirmed",
                evidence_url="https://example.org/proof",
            ),
            RequirementCheck(
                requirement="сырьё с повышенным содержанием серы",
                status="not_confirmed",
            ),
        ],
    )
    report = _make_report([card], brief=brief)

    text = render_markdown(report)

    assert "**Сверка с требованиями задачи:**" in text
    assert "работа при 340 °C: подтверждено источником ([ссылка](https://example.org/proof))" in text
    assert "сырьё с повышенным содержанием серы: не подтверждено источником" in text


def test_requirement_checks_block_absent_when_empty(brief):
    """Блок не печатается, если сверки по требованиям нет вовсе."""
    card = _make_card(requirement_checks=[])
    report = _make_report([card], brief=brief)

    text = render_markdown(report)

    assert "Сверка с требованиями задачи" not in text


def test_unsourced_claims_visible_near_card(brief):
    """unsourced_claims виден в тексте карточки, а не только в рисках."""
    card = _make_card(unsourced_claims=["Утверждение о сроке окупаемости не подтверждено источником"])
    report = _make_report([card], brief=brief)

    text = render_markdown(report)

    assert "**Не подтверждено проверкой:**" in text
    assert "Утверждение о сроке окупаемости не подтверждено источником" in text


def test_unsourced_claims_block_absent_when_empty(brief):
    card = _make_card(unsourced_claims=[])
    report = _make_report([card], brief=brief)

    text = render_markdown(report)

    assert "Не подтверждено проверкой" not in text


def test_pdf_urls_printed_as_full_text_link(brief):
    """pdf_urls непуст — в тексте появляется строка со ссылкой на полный текст."""
    card = _make_card(pdf_urls=["https://arxiv.org/pdf/example.pdf"])
    report = _make_report([card], brief=brief)

    text = render_markdown(report)

    assert "Полный текст: [PDF](https://arxiv.org/pdf/example.pdf)" in text


def test_pdf_urls_absent_when_empty(brief):
    card = _make_card(pdf_urls=[])
    report = _make_report([card], brief=brief)

    text = render_markdown(report)

    assert "Полный текст:" not in text


def test_sources_unavailable_line_not_broken(brief):
    """Регрессия: строка «Не ответили источники» по-прежнему печатается при непустом списке."""
    card = _make_card()
    report = _make_report([card], brief=brief, sources_unavailable=["Роспатент", "YandexGPT"])

    text = render_markdown(report)

    assert "Не ответили источники: Роспатент, YandexGPT" in text


_CAVEAT = "Уточнения по задаче не собраны: бриф составлен из текста задачи."


@pytest.mark.parametrize(
    ("sources_unavailable", "caveats"),
    [
        (["Роспатент"], []),
        ([], [_CAVEAT]),
        (["Роспатент"], [_CAVEAT]),
    ],
    ids=["только-источники", "только-оговорки", "и-то-и-другое"],
)
def test_limitations_heading_printed_whenever_section_has_content(brief, sources_unavailable, caveats):
    """Заголовок «Ограничения этого поиска» нужен во всех трёх случаях. Раньше
    он был привязан только к недоступным источникам, и на здоровом прогоне
    (все источники ответили) оговорка выпадала в конец отчёта голым пунктом
    списка — клиент видел висящую строку непонятно к чему."""
    report = _make_report(
        [_make_card()],
        brief=brief,
        sources_unavailable=sources_unavailable,
        summary=ReportSummary(recommendation="Начать с варианта №1.", caveats=caveats),
    )

    text = render_markdown(report)
    lines = text.split("\n")

    assert "## Ограничения этого поиска" in lines
    heading_at = lines.index("## Ограничения этого поиска")
    for caveat in caveats:
        # Оговорка обязана стоять ПОСЛЕ заголовка, а не болтаться отдельно.
        assert lines.index(f"- {caveat}") > heading_at
    if sources_unavailable:
        assert any(line.startswith("Не ответили источники: ") for line in lines[heading_at:])


def test_limitations_heading_absent_when_nothing_to_say(brief):
    """Обратная сторона: без недоступных источников и без оговорок раздела нет
    вовсе — пустой заголовок читался бы как недосказанность."""
    report = _make_report([_make_card()], brief=brief)

    assert "## Ограничения этого поиска" not in render_markdown(report)


@pytest.mark.parametrize("stage", list(MaturityStage))
def test_all_maturity_stages_have_russian_label(stage):
    """MATURITY_RU покрывает все значения перечисления, включая unknown."""
    from app.export.markdown import MATURITY_RU

    assert stage in MATURITY_RU
