"""Проверки наложения правок оператора поверх report_json (И-7, РЕШ-186, РЕШ-148).

Слой чистый: без БД и без сети. Каждый тест собирает `Report` из fixtures и
проверяет, что наложение не выходит за белый список полей и не мутирует
исходный объект.
"""

from __future__ import annotations

from datetime import datetime

from app.report_overrides import EDITABLE_FIELDS, apply_overrides
from app.schemas import Confidence, Evidence, Report, ReportSummary, SolutionCard


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


def _make_report(cards: list[SolutionCard], contest_cards: list[SolutionCard] | None = None, **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": cards,
        "contest_cards": contest_cards or [],
        "summary": ReportSummary(recommendation="Начать с варианта №1."),
        "generated_at": datetime(2026, 8, 12, 10, 0),
    }
    fields.update(overrides)
    return Report(**fields)


def test_empty_overrides_give_byte_identical_report(brief):
    """Пустой словарь правок не меняет отчёт — сериализация совпадает побайтово."""
    report = _make_report([_make_card()], brief=brief)

    result = apply_overrides(report, {})

    assert result.model_dump_json() == report.model_dump_json()


def test_text_field_is_replaced(brief):
    """Текстовое поле из белого списка подменяется значением из правки."""
    report = _make_report([_make_card(title="Исходный заголовок")], brief=brief)

    result = apply_overrides(report, {(False, 0): {"title": "Заголовок оператора"}})

    assert result.cards[0].title == "Заголовок оператора"


def test_original_report_is_not_mutated(brief):
    """Исходный `Report`, переданный в функцию, остаётся неизменным."""
    report = _make_report([_make_card(title="Исходный заголовок")], brief=brief)

    apply_overrides(report, {(False, 0): {"title": "Заголовок оператора"}})

    assert report.cards[0].title == "Исходный заголовок"


def test_untouched_card_nested_fields_do_not_leak_into_source(brief):
    """`model_copy(deep=True)` защищает вложенные изменяемые поля непереписанной
    карточки: правка карточки 0 не должна оставлять `result.cards[1]` тем же
    объектом, что `report.cards[1]`, иначе мутация результата бесшумно портит
    исходный отчёт (найдено ревью цикла 1, критерий приёмки 1)."""
    untouched = _make_card(title="Вторая карточка", risks=["исходный риск"])
    report = _make_report([_make_card(title="Первая карточка"), untouched], brief=brief)

    result = apply_overrides(report, {(False, 0): {"title": "Правка первой"}})

    assert result.cards[1] is not report.cards[1]
    assert result.cards[1].risks is not report.cards[1].risks

    result.cards[1].risks.append("мутация результата")

    assert report.cards[1].risks == ["исходный риск"]


def test_contest_card_at_same_position_is_untouched(brief):
    """Ключ — пара (is_contest, position): правка обычной карточки не задевает
    конкурсную карточку с тем же порядковым номером."""
    report = _make_report(
        [_make_card(title="Обычная карточка")],
        contest_cards=[_make_card(title="Конкурсная карточка")],
        brief=brief,
    )

    result = apply_overrides(report, {(False, 0): {"title": "Правка обычной"}})

    assert result.cards[0].title == "Правка обычной"
    assert result.contest_cards[0].title == "Конкурсная карточка"


def test_confidence_override_is_ignored(brief):
    """Попытка подменить confidence через наложение молча игнорируется (РЕШ-148)."""
    card = _make_card(confidence=Confidence.MEDIUM)
    report = _make_report([card], brief=brief)

    result = apply_overrides(report, {(False, 0): {"confidence": "high", "title": "Новый заголовок"}})

    assert result.cards[0].confidence == Confidence.MEDIUM
    assert result.cards[0].title == "Новый заголовок"


def test_requirement_checks_and_unsourced_claims_override_is_ignored(brief):
    """`requirement_checks` и `unsourced_claims` не редактируются никогда (РЕШ-148)."""
    card = _make_card(requirement_checks=[], unsourced_claims=["неподтверждённое утверждение"])
    report = _make_report([card], brief=brief)

    result = apply_overrides(
        report,
        {
            (False, 0): {
                "requirement_checks": [{"requirement": "x", "status": "confirmed"}],
                "unsourced_claims": [],
            }
        },
    )

    assert result.cards[0].requirement_checks == []
    assert result.cards[0].unsourced_claims == ["неподтверждённое утверждение"]


def test_evidence_removed_drops_exactly_one_row(brief):
    """`evidence_removed` убирает ровно одну строку подтверждений по URL."""
    card = _make_card(
        evidence=[
            Evidence(claim="Факт один", url="https://example.org/one"),
            Evidence(claim="Факт два", url="https://example.org/two"),
        ]
    )
    report = _make_report([card], brief=brief)

    result = apply_overrides(report, {(False, 0): {"evidence_removed": ["https://example.org/one"]}})

    assert len(result.cards[0].evidence) == 1
    assert str(result.cards[0].evidence[0].url) == "https://example.org/two"


def test_cannot_add_evidence_via_override(brief):
    """Добавить строку evidence наложением невозможно — в коде нет такого пути."""
    card = _make_card(evidence=[Evidence(claim="Факт один", url="https://example.org/one")])
    report = _make_report([card], brief=brief)

    result = apply_overrides(
        report,
        {
            (False, 0): {
                "evidence": [
                    {"claim": "Выдуманный факт", "url": "https://example.org/fake"},
                ]
            }
        },
    )

    assert len(result.cards[0].evidence) == 1
    assert str(result.cards[0].evidence[0].url) == "https://example.org/one"


def test_all_editable_fields_are_applied(brief):
    """Все поля из белого списка действительно применяются наложением."""
    card = _make_card()
    report = _make_report([card], brief=brief)
    patch = {
        "title": "Новый заголовок",
        "essence": "Новая суть",
        "applicability": "Новая применимость",
        "next_step": "Новый шаг",
        "risks": ["новый риск"],
        "contact_leads": ["novy@example.org"],
        "developer_org": "Новая организация",
        "developer_team": "Новая команда",
    }
    assert set(patch) == set(EDITABLE_FIELDS)

    result = apply_overrides(report, {(False, 0): patch})

    for field, value in patch.items():
        assert getattr(result.cards[0], field) == value


def test_unknown_key_in_overrides_map_is_ignored(brief):
    """Правка по позиции, которой нет в отчёте, ни на что не влияет и не падает."""
    report = _make_report([_make_card()], brief=brief)

    result = apply_overrides(report, {(False, 5): {"title": "Мимо"}, (True, 0): {"title": "Мимо"}})

    assert result.cards[0].title == "Регенерация катализатора добавками молибдена"
