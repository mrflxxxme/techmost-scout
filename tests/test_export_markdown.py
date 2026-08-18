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


# ── Секция «Ранние разработки и команды» (Т-24, РЕШ-85 п. 1, РЕШ-79 п. 1) ─────

_CONTEST_HEADING = "## Ранние разработки и команды"
_MAIN_HEADING = "## Варианты решений"


def _section_card_titles(text: str, heading: str) -> list[str]:
    """Заголовки карточек `### …` внутри секции, ограниченной следующим `## `."""
    lines = text.split("\n")
    start = lines.index(heading)
    titles: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("### "):
            titles.append(line)
    return titles


def _contest_card(number: int, **overrides) -> SolutionCard:
    """Карточка конкурсного класса — то, что попадает в отдельную секцию."""
    fields = {
        "title": f"Хакатонный прототип №{number}",
        "essence": "Команда собрала работающий прототип за 48 часов.",
        "applicability": "Закрывает ту же задачу дешёвым способом.",
        "next_step": "Связаться с капитаном команды.",
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def test_contest_section_does_not_change_main_section_count(brief):
    """Критерий 1: конкурсные карточки идут вне лимита основной выдачи.

    Шесть обычных карточек остаются шестью в «Вариантах решений», две
    конкурсные печатаются отдельной секцией. Если конкурсные попадут в лимит —
    это вытеснение зрелого решения, ровно то, что владелец отверг (РЕШ-85 п. 1).
    """
    main = [_make_card(title=f"Решение №{n}") for n in range(1, 7)]
    contest = [_contest_card(1), _contest_card(2)]
    report = _make_report(main, brief=brief)

    text = render_markdown(report, contest_cards=contest)

    assert _CONTEST_HEADING in text.split("\n")
    assert len(_section_card_titles(text, _MAIN_HEADING)) == 6
    assert len(_section_card_titles(text, _CONTEST_HEADING)) == 2


def test_contest_section_goes_after_main_section(brief):
    """Секция стоит после основных карточек, а не вместо них (РЕШ-85 п. 1)."""
    report = _make_report([_make_card()], brief=brief)

    lines = render_markdown(report, contest_cards=[_contest_card(1)]).split("\n")

    assert lines.index(_MAIN_HEADING) < lines.index(_CONTEST_HEADING)


def test_contest_section_absent_without_contest_cards(brief):
    """Критерий 2: пустая секция не печатается вовсе — заголовок без карточек
    есть обещание, которого отчёт не выполняет."""
    report = _make_report([_make_card()], brief=brief)

    assert _CONTEST_HEADING not in render_markdown(report)
    assert _CONTEST_HEADING not in render_markdown(report, contest_cards=[])


def test_ip_status_line_printed_in_every_contest_card(brief):
    """Критерий 3: строка статуса прав есть у КАЖДОЙ карточки секции, в том
    числе когда площадка неизвестна (РЕШ-79 п. 1)."""
    with_platform = _contest_card(1, candidate_urls=["https://hacks-ai.ru/event/12"])
    without_platform = _contest_card(2, candidate_urls=[])
    report = _make_report([_make_card()], brief=brief)

    text = render_markdown(report, contest_cards=[with_platform, without_platform])
    section = text.split(_CONTEST_HEADING, 1)[1]

    assert section.count("**Статус прав:**") == 2
    assert section.count("статус прав не подтверждён, требует выяснения у команды") == 2
    # Значение по умолчанию печатается словами — честный отказ, а не догадка
    # (Т-22, критерий 4: печать проверяет Т-24).
    assert section.count("**Статус прав:** не установлено") == 2
    assert section.count("(площадка: hacks-ai.ru)") == 1


def test_ip_status_line_absent_in_main_section(brief):
    """Обычная карточка строки статуса прав не несёт: она про конкурсный класс."""
    report = _make_report([_make_card()], brief=brief)

    assert "Статус прав" not in render_markdown(report)


def test_contest_card_without_evidence_hides_maturity_and_trl(brief):
    """Критерий 4, двойник проверки инварианта №1 для новой секции: карточка с
    пустым evidence не печатает ни стадии зрелости, ни TRL и в этой секции."""
    contest = _contest_card(1, maturity_stage=MaturityStage.PROTOTYPE, trl_estimate=4, evidence=[])
    report = _make_report([_make_card()], brief=brief)

    text = render_markdown(report, contest_cards=[contest])
    section = text.split(_CONTEST_HEADING, 1)[1]

    assert "Стадия зрелости" not in section
    assert "TRL" not in section
    assert "Подтверждений не приложено" in section


def test_contest_card_with_evidence_still_shows_maturity(brief):
    """Обратная сторона: подтверждённая конкурсная карточка стадию печатает."""
    contest = _contest_card(
        1,
        maturity_stage=MaturityStage.PROTOTYPE,
        trl_estimate=4,
        evidence=[Evidence(claim="Победитель хакатона", url="https://hacks-ai.ru/w")],
    )
    report = _make_report([_make_card()], brief=brief)

    text = render_markdown(report, contest_cards=[contest])
    section = text.split(_CONTEST_HEADING, 1)[1]

    assert "**Стадия зрелости:** есть опытный образец (TRL ≈ 4)" in section


def test_draft_header_untouched_by_contest_section(brief):
    """Инвариант №2: шапка «ЧЕРНОВИК» остаётся на месте и с новой секцией."""
    report = _make_report([_make_card()], brief=brief)

    text = render_markdown(report, contest_cards=[_contest_card(1)])

    assert "**ЧЕРНОВИК — не проверено экспертом.**" in text
