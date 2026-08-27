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
