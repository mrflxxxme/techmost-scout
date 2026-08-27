"""Пополевые ворота печати сводки в печатном HTML/PDF (РЕШ-211, РЕШ-212).

Вынесено из `tests/test_export_pdf.py`, чтобы не приближать тот файл к
пределу 500 строк раньше времени — новое поведение получает отдельный файл
(страж `tests/test_tests_layout.py` не тронут). Сеть не задействована
(инвариант №7): `render_print_html` работает на чистом `ExportView`.
"""

from __future__ import annotations

from datetime import datetime

from app.export.markdown import (
    COMPARISON_TABLE_HIDDEN_NOTICE,
    SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE,
    SUMMARY_NOT_PRINTABLE_NOTICE,
)
from app.export.pdf import render_print_html
from app.export.view import ExportView
from app.schemas import Brief, Evidence, MaturityStage, Report, ReportSummary, SolutionCard


def _brief(**overrides) -> Brief:
    fields = {
        "industry": "нефтепереработка",
        "task_summary": "Продлить межрегенерационный цикл катализатора крекинга.",
    }
    fields.update(overrides)
    return Brief(**fields)


def _card(**overrides) -> SolutionCard:
    fields = {
        "title": "Регенерация катализатора добавками молибдена",
        "essence": "Продление срока службы катализатора за счёт присадки.",
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "next_step": "Запросить образец у разработчика.",
        "maturity_stage": MaturityStage.PILOT,
        "trl_estimate": 6,
        "evidence": [Evidence(claim="Испытан на действующей установке", url="https://example.org/a")],
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _report(cards: list[SolutionCard], **overrides) -> Report:
    fields = {
        "request_id": 42,
        "brief": _brief(),
        "cards": cards,
        "contest_cards": [],
        "summary": ReportSummary(recommendation="Начать с варианта №1."),
        "generated_at": datetime(2026, 8, 27, 9, 0),
    }
    fields.update(overrides)
    return Report(**fields)


def _view(report: Report, **overrides) -> ExportView:
    fields = {
        "request_id": report.request_id,
        "report": report,
        "card_ids": {(False, i): i + 1 for i in range(len(report.cards))},
        "is_draft": True,
        "reviewer_name": "",
        "validated_at": None,
        "rework_keys": set(),
        "total_cards": len(report.cards) + len(report.contest_cards),
        "recommendation_edited": False,
        "caveats_edited": False,
        "comparison_table_hidden": False,
    }
    fields.update(overrides)
    return ExportView(**fields)


def test_правка_только_оговорок_печатает_оговорки_не_рекомендацию():
    """Критерий 2: эксперт правил только оговорки — проза рекомендации модели
    не печатается, оговорка эксперта печатается."""
    report = _report(
        [_card()],
        summary=ReportSummary(recommendation="Первым берём вариант 1.", caveats=["Оговорка эксперта: проверил лично."]),
    )
    view = _view(report, total_cards=2, caveats_edited=True)

    html = render_print_html(view)

    assert "Первым берём вариант 1." not in html
    assert "Оговорка эксперта: проверил лично." in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE in html


def test_правка_только_рекомендации_печатает_рекомендацию_не_оговорки():
    """Зеркало критерия 2: эксперт правил только рекомендацию — оговорки
    модели не печатаются, рекомендация эксперта печатается целиком."""
    report = _report(
        [_card()],
        summary=ReportSummary(
            recommendation="Начать со второго варианта.", caveats=["В карточке 3 риск не подтверждён."]
        ),
    )
    view = _view(report, total_cards=2, recommendation_edited=True)

    html = render_print_html(view)

    assert "Начать со второго варианта." in html
    assert "В карточке 3 риск не подтверждён." not in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in html


def test_оба_поля_правлены_печатается_всё():
    """Критерий 3: эксперт правил оба поля — печатается всё, что он написал."""
    report = _report(
        [_card()],
        summary=ReportSummary(recommendation="Текст эксперта.", caveats=["Оговорка эксперта."]),
    )
    view = _view(report, total_cards=2, recommendation_edited=True, caveats_edited=True)

    html = render_print_html(view)

    assert "Текст эксперта." in html
    assert "Оговорка эксперта." in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in html


def test_ничего_не_скрыто_печатается_всё_как_раньше():
    """Критерий 4: ни одна карточка не скрыта — оба поля печатаются без правки."""
    report = _report(
        [_card()],
        summary=ReportSummary(recommendation="Рекомендация модели.", caveats=["Оговорка модели."]),
    )
    view = _view(report)

    html = render_print_html(view)

    assert "Рекомендация модели." in html
    assert "Оговорка модели." in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in html


def test_comparison_table_hidden_печатает_честную_строку_вместо_таблицы():
    """РЕШ-212: таблица спрятана — на её месте честная строка, а не пустота."""
    report = _report([_card()], summary=ReportSummary(recommendation="x", comparison_rows=[]))
    view = _view(report, total_cards=2, comparison_table_hidden=True)

    html = render_print_html(view)

    assert COMPARISON_TABLE_HIDDEN_NOTICE in html
    assert "<table" not in html


def test_comparison_table_hidden_заглушка_рекомендации_не_ссылается_на_таблицу():
    """РЕШ-212, П-14 финального аудита: если таблица спрятана, заглушка вместо
    рекомендации не отсылает клиента к разделу, которого в документе нет."""
    report = _report(
        [_card()],
        summary=ReportSummary(recommendation="Первым берём вариант 1.", comparison_rows=[]),
    )
    view = _view(report, total_cards=2, comparison_table_hidden=True)

    html = render_print_html(view)

    assert SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in html
    assert "Ориентируйтесь на таблицу сравнения" not in html


def test_comparison_table_not_hidden_заглушка_ссылается_на_таблицу_как_раньше():
    """Обратная сторона: таблица напечатана — прежняя формулировка заглушки
    (со ссылкой на таблицу) остаётся в силе."""
    report = _report(
        [_card()],
        summary=ReportSummary(
            recommendation="Первым берём вариант 1.",
            comparison_rows=[{"option": "Вариант 1"}],
        ),
    )
    view = _view(report, total_cards=2, comparison_table_hidden=False)

    html = render_print_html(view)

    assert SUMMARY_NOT_PRINTABLE_NOTICE in html
    assert SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE not in html
    assert COMPARISON_TABLE_HIDDEN_NOTICE not in html


def test_comparison_table_hidden_но_рекомендация_правлена_заглушки_нет_вовсе():
    """Рекомендацию правил эксперт — заглушка не нужна вовсе, даже если таблица
    спрятана: честная строка на месте таблицы при этом остаётся."""
    report = _report(
        [_card()],
        summary=ReportSummary(recommendation="Текст эксперта.", comparison_rows=[]),
    )
    view = _view(report, total_cards=2, recommendation_edited=True, comparison_table_hidden=True)

    html = render_print_html(view)

    assert "Текст эксперта." in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in html
    assert SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE not in html
    assert COMPARISON_TABLE_HIDDEN_NOTICE in html


def test_no_table_notice_не_упоминает_таблицу_сравнения():
    """РЕШ-212: заглушка «таблица спрятана» не отсылает к разделу, которого нет
    в документе — в отличие от обычной заглушки."""
    assert "таблицу сравнения" in SUMMARY_NOT_PRINTABLE_NOTICE
    assert "таблиц" not in SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE


def test_comparison_table_not_hidden_и_строк_нет_секции_вовсе_не_печатается():
    """Пустая сводка модели с самого начала (`comparison_table_hidden=False`,
    строк нет) — раздел «Сравнение вариантов» не печатается вовсе, как и до
    правки РЕШ-212."""
    report = _report([_card()], summary=ReportSummary(recommendation="x", comparison_rows=[]))
    view = _view(report)

    html = render_print_html(view)

    assert "Сравнение вариантов" not in html
    assert COMPARISON_TABLE_HIDDEN_NOTICE not in html
