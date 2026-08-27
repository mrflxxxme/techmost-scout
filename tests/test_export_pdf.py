"""Проверки печатного шаблона и выгрузки в PDF (Э4-И4, SPEC Э4-2 часть 1).

Браузер в наборе не поднимается (РЕШ-196 п. 12): `export_pdf` вызывается с
подменённой страницей (`page.pdf` возвращает готовые байты), а печатный HTML
проверяется разбором строки — без запуска Chromium. Сеть не задействована
(инвариант №7).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.export.markdown import SUMMARY_NOT_PRINTABLE_NOTICE
from app.export.pdf import export_pdf, render_print_html
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
        "cost_rub": 3.5,
    }
    fields.update(overrides)
    return Report(**fields)


def _view(
    report: Report,
    *,
    is_draft: bool,
    reviewer_name: str = "",
    validated_at: datetime | None = None,
    **overrides,
) -> ExportView:
    fields = {
        "request_id": report.request_id,
        "report": report,
        "card_ids": {(False, i): i + 1 for i in range(len(report.cards))},
        "is_draft": is_draft,
        "reviewer_name": reviewer_name,
        "validated_at": validated_at,
        "rework_keys": set(),
        # По умолчанию ничего не скрыто (РЕШ-206): число карточек до фильтра
        # совпадает с числом карточек в переданном отчёте — тесты, которым
        # важна ветка «сводка не печатается», задают `total_cards` сами.
        "total_cards": len(report.cards) + len(report.contest_cards),
        "summary_text_edited": False,
    }
    fields.update(overrides)
    return ExportView(**fields)


class _FakePage:
    """Заглушка страницы Chromium: считает вызовы, не трогает сеть и браузер."""

    def __init__(self) -> None:
        self.set_content_calls: list[str] = []
        self.pdf_calls: list[dict[str, object]] = []

    async def set_content(self, html: str) -> None:
        self.set_content_calls.append(html)

    async def pdf(self, **kwargs: object) -> bytes:
        self.pdf_calls.append(kwargs)
        return b"%PDF-1.4 fake"


def test_render_print_html_draft_has_repeating_mark_and_no_signature():
    """Черновик: пометка на обложке и в повторяющемся элементе, подписи нет."""
    html = render_print_html(_view(_report([_card()]), is_draft=True))
    assert html.count("ЧЕРНОВИК — не проверено экспертом") == 2
    assert 'class="print-cover-draft"' in html
    assert 'class="print-draft-mark"' in html
    assert "проверено:" not in html


def test_render_print_html_validated_has_exact_footer_and_no_draft():
    """После валидации — ровно строка РЕШ-197 и ни одного «ЧЕРНОВИК»."""
    when = datetime(2026, 8, 28, 12, 0)
    view = _view(_report([_card()]), is_draft=False, reviewer_name="Мария Соколова", validated_at=when)
    html = render_print_html(view)
    expected = "Подготовлено системой ТехМост Скаут, проверено: Мария Соколова, 28.08.2026"
    assert html.count(expected) == 1
    assert "ЧЕРНОВИК" not in html


def test_render_print_html_empty_evidence_card_is_not_printed():
    """РЕШ-204: карточка без единого подтверждения в печать не попадает вовсе."""
    bare = _card(title="Карточка без подтверждений", evidence=[], trl_estimate=None)
    html = render_print_html(_view(_report([bare]), is_draft=True))
    assert "Карточка без подтверждений" not in html
    assert "Стадия зрелости" not in html
    assert "TRL" not in html
    assert "Нельзя показывать клиенту: нет подтверждений" not in html
    assert "Пригодных вариантов не найдено." in html


def test_render_print_html_mixed_evidence_keeps_order_of_visible_cards():
    """Смешанный список: карточка без evidence выпадает, соседние сохраняют порядок."""
    first = _card(title="Первая с подтверждением")
    bare = _card(title="Вторая без подтверждения", evidence=[])
    third = _card(title="Третья с подтверждением")
    html = render_print_html(_view(_report([first, bare, third]), is_draft=True))
    assert "Вторая без подтверждения" not in html
    assert html.index("Первая с подтверждением") < html.index("Третья с подтверждением")


def test_render_print_html_contest_section_hidden_when_all_bare():
    """Если у всех конкурсных карточек пустой evidence — секция вовсе не печатается."""
    bare_contest = _card(title="Конкурсная без подтверждения", evidence=[])
    view = _view(_report([_card()], contest_cards=[bare_contest]), is_draft=True)
    html = render_print_html(view)
    assert "Конкурсная без подтверждения" not in html
    assert "Ранние разработки и команды" not in html


def test_template_never_uses_markdown_empty_evidence_phrase():
    """Фраза markdown-экспорта «Подтверждений не приложено» в печатный шаблон не попадает."""
    path = Path(__file__).resolve().parents[1] / "backend/app/web/templates/report_print.html"
    assert "Подтверждений не приложено" not in path.read_text(encoding="utf-8")


def test_render_print_html_rework_mark_stays_operator_only():
    """РЕШ-198: пометка «на доработку» — только для оператора, в PDF её нет."""
    card = _card(title="Карточка на доработке")
    view = _view(_report([card]), is_draft=True, rework_keys={(False, 0)})
    html = render_print_html(view)
    assert "На доработку" not in html
    assert "Карточка на доработке" in html


def test_render_print_html_links_are_anchors():
    """Ссылки источников печатаются тегом <a href> — в PDF они кликабельны."""
    html = render_print_html(_view(_report([_card()]), is_draft=True))
    assert '<a href="https://example.org/a">источник</a>' in html


async def test_export_pdf_calls_page_pdf_exactly_once(tmp_path: Path):
    """`export_pdf` с подменённой страницей вызывает `page.pdf` ровно один раз."""
    page = _FakePage()
    out_dir = tmp_path / "out"
    out_path = out_dir / "report-42-2026-08-27.pdf"

    view = _view(_report([_card()]), is_draft=True)
    result = await export_pdf(view, out_path, page=page)

    assert result == out_path
    assert len(page.pdf_calls) == 1
    assert len(page.set_content_calls) == 1
    assert page.pdf_calls[0]["format"] == "A4"
    assert page.pdf_calls[0]["print_background"] is True
    assert out_path.exists()
    assert out_path.read_bytes() == b"%PDF-1.4 fake"


async def test_export_pdf_creates_parent_directory(tmp_path: Path):
    """Каталог `out/` создаётся, если его ещё нет — команда выгрузки его не готовит заранее."""
    page = _FakePage()
    out_path = tmp_path / "не-существует-заранее" / "report-42-2026-08-27.pdf"

    await export_pdf(_view(_report([_card()]), is_draft=True), out_path, page=page)

    assert out_path.exists()


@pytest.mark.parametrize("is_draft", [True, False])
async def test_export_pdf_html_matches_render_print_html(tmp_path: Path, is_draft: bool):
    """HTML, ушедший в `page.set_content`, — ровно то, что строит `render_print_html`."""
    page = _FakePage()
    view = _view(
        _report([_card()]),
        is_draft=is_draft,
        reviewer_name="Пётр Иванов" if not is_draft else "",
        validated_at=datetime(2026, 8, 28, 10, 0) if not is_draft else None,
    )
    await export_pdf(view, tmp_path / "report-42-2026-08-27.pdf", page=page)
    assert page.set_content_calls[0] == render_print_html(view)


def test_render_print_html_draft_with_verdict_does_not_print_signature():
    """П-1 аудита: мутация «печатать подпись без проверки is_draft» убита —
    даже при заполненном `validated_at` черновик подписи не печатает."""
    view = _view(
        _report([_card()]),
        is_draft=True,
        reviewer_name="Мария Соколова",
        validated_at=datetime(2026, 8, 28, 12, 0),
    )
    html = render_print_html(view)
    assert "проверено:" not in html
    assert "ЧЕРНОВИК — не проверено экспертом" in html


def test_render_print_html_no_cost_line_in_footer():
    """РЕШ-207: строки о стоимости машинной обработки в клиентском PDF нет."""
    html = render_print_html(_view(_report([_card()], cost_rub=11.3711), is_draft=True))
    assert "стоимость" not in html
    assert "₽" not in html
    assert "Запрос №42 · 27.08.2026 09:00" in html


def test_render_print_html_comparison_table_headers_are_russian():
    """РЕШ-207: латинские ключи таблицы сравнения печатаются русскими заголовками."""
    report = _report(
        [_card()],
        summary=ReportSummary(
            recommendation="Начать с варианта №1.",
            comparison_rows=[
                {"option": "Молибден", "maturity": "pilot", "gives": "испытан", "needs_from_client": "образец"}
            ],
        ),
    )
    html = render_print_html(_view(report, is_draft=True))
    assert "<th>Вариант</th>" in html
    assert "<th>Стадия зрелости</th>" in html
    assert "<th>Что подтверждено</th>" in html
    assert "<th>Что нужно от вас</th>" in html
    assert "<th>option</th>" not in html


def test_render_print_html_summary_not_printable_when_card_hidden_and_not_edited():
    """РЕШ-206: карточка скрыта (total_cards больше печатаемых), правки нет —
    рекомендация и оговорки модели не печатаются, заглушка есть."""
    report = _report(
        [_card()],
        summary=ReportSummary(recommendation="Первым берём вариант 1.", caveats=["В карточке 3 риск не подтверждён."]),
    )
    view = _view(report, is_draft=True, total_cards=2, summary_text_edited=False)

    html = render_print_html(view)

    assert "Первым берём вариант 1." not in html
    assert "В карточке 3 риск не подтверждён." not in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE in html


def test_render_print_html_summary_not_printable_when_hidden_by_empty_evidence():
    """РЕШ-206, регрессия ревью цикла 2: total_cards совпадает с числом карточек
    в отчёте (reject-вердикта нет вовсе), но одна карточка скрывается самим
    экспортёром из-за пустого evidence (РЕШ-204) — рекомендация и оговорки всё
    равно не печатаются. Мутация «считать printed_cards по нефильтрованным
    report.cards/contest_cards» сделала бы printed_cards == total_cards и
    напечатала бы текст модели — эта проверка её убивает."""
    bare = _card(title="Карточка без подтверждений", evidence=[], trl_estimate=None)
    report = _report(
        [_card(), bare],
        summary=ReportSummary(recommendation="Первым берём вариант 1.", caveats=["В карточке 3 риск не подтверждён."]),
    )
    view = _view(report, is_draft=True, total_cards=2, summary_text_edited=False)

    html = render_print_html(view)

    assert "Первым берём вариант 1." not in html
    assert "В карточке 3 риск не подтверждён." not in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE in html


def test_render_print_html_sources_unavailable_line_survives_hidden_summary():
    """Инвариант №5: строка о недоступных источниках остаётся, даже когда
    рекомендация и оговорки скрыты из-за отклонённой карточки."""
    report = _report(
        [_card()],
        summary=ReportSummary(recommendation="Первым берём вариант 1.", caveats=["Оговорка модели."]),
        sources_unavailable=["cyberleninka"],
    )
    view = _view(report, is_draft=True, total_cards=2, summary_text_edited=False)

    html = render_print_html(view)

    assert "Не ответили источники: cyberleninka" in html
    assert "Оговорка модели." not in html


def test_render_print_html_summary_printable_when_expert_edited_recommendation():
    """РЕШ-206 (зеркало): карточка скрыта, но эксперт правил сводку — печатается
    его текст целиком, включая оговорки."""
    report = _report(
        [_card()],
        summary=ReportSummary(recommendation="Текст эксперта.", caveats=["Правленая оговорка."]),
    )
    view = _view(report, is_draft=True, total_cards=2, summary_text_edited=True)

    html = render_print_html(view)

    assert "Текст эксперта." in html
    assert "Правленая оговорка." in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in html


def test_сквозной_отчёт_с_отклонённой_карточкой_без_правки_сводки():
    """Сквозной сценарий: отчёт из 4 карточек, одна отклонена экспертом
    (`build_export_view` её уже убрал бы) — в выгрузке 3 карточки, 3 строки
    сравнения, текста рекомендации нет, оговорок нет, строка о недоступных
    источниках на месте, стоимости нет, шапка таблицы русская."""
    visible_cards = [_card(title=f"Карточка №{n}") for n in (1, 2, 3)]
    report = _report(
        visible_cards,
        summary=ReportSummary(
            recommendation="Первым берём вариант 1.",
            caveats=["В карточке 3 риск не подтверждён."],
            comparison_rows=[
                {"option": "Вариант 1", "maturity": "pilot", "gives": "испытан", "needs_from_client": "образец"},
                {"option": "Вариант 2", "maturity": "lab", "gives": "статья", "needs_from_client": "команда"},
                {"option": "Вариант 3", "maturity": "series", "gives": "патент", "needs_from_client": "лицензия"},
            ],
        ),
        sources_unavailable=["cyberleninka"],
        cost_rub=11.3711,
    )
    view = _view(report, is_draft=True, total_cards=4, summary_text_edited=False)

    html = render_print_html(view)

    assert html.count('<div class="print-card">') == 3
    assert html.count("<tr>") == 4  # заголовок + 3 строки сравнения
    assert "Первым берём вариант 1." not in html
    assert "В карточке 3 риск не подтверждён." not in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE in html
    assert "Не ответили источники: cyberleninka" in html
    assert "стоимость" not in html
    assert "₽" not in html
    assert "<th>Вариант</th>" in html


def test_сквозной_отчёт_с_отклонённой_карточкой_эксперт_правил_сводку():
    """Зеркальный сценарий: та же ситуация, но эксперт правил сводку —
    печатается его текст целиком."""
    visible_cards = [_card(title=f"Карточка №{n}") for n in (1, 2, 3)]
    report = _report(
        visible_cards,
        summary=ReportSummary(
            recommendation="Текст, который правил эксперт.",
            caveats=["Оговорка, которую эксперт оставил."],
            comparison_rows=[
                {"option": "Вариант 1"},
                {"option": "Вариант 2"},
                {"option": "Вариант 3"},
            ],
        ),
    )
    view = _view(report, is_draft=True, total_cards=4, summary_text_edited=True)

    html = render_print_html(view)

    assert "Текст, который правил эксперт." in html
    assert "Оговорка, которую эксперт оставил." in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in html
