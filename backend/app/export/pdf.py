"""Печатный шаблон отчёта и выгрузка в PDF (SPEC Э4-2, часть 1, РЕШ-196 п. 7).

`render_print_html` собирает самостоятельный HTML-документ из `ExportView` —
без наследования `base.html` (у печати нет ни шапки, ни навигации) и без
ссылок на `/static`: `page.set_content` не резолвит относительные адреса,
поэтому стили `print.css` читаются с диска и вставляются содержимым прямо в
`<style>`. Человеческие строки (стадии зрелости, уверенность, заголовок
секции конкурсных решений, строка ограничений) берутся из `app.export.markdown`
— той же точкой правды, что пользуются markdown-экспорт и страница отчёта.

`export_pdf` печатает готовый HTML через Chromium (playwright, группа
зависимостей `export`). Браузер запускается только тогда, когда вызывающий
не передал готовую `page` сам, — так тесты подменяют весь объект страницы
(нужен только `page.pdf`) и не поднимают ни одного процесса Chromium
(РЕШ-196 п. 12): в машине, где выполняются тесты, браузерных бинарников нет
вовсе, и попытка запустить его уронила бы набор при первом же прогоне.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.export.markdown import (
    COMPARISON_HEADERS_RU,
    COMPARISON_TABLE_HIDDEN_NOTICE,
    CONFIDENCE_RU,
    CONTEST_SECTION_HEADING,
    MATURITY_RU,
    SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE,
    SUMMARY_NOT_PRINTABLE_NOTICE,
    TEMPLATE_FILTERS,
    limitations_lines,
)
from app.export.view import ExportView, caveats_printable, recommendation_printable
from app.verdicts import signature_line

_WEB_DIR = Path(__file__).resolve().parents[1] / "web"
_TEMPLATES_DIR = _WEB_DIR / "templates"
_PRINT_CSS_PATH = _WEB_DIR / "static" / "print.css"


class PrintPage(Protocol):
    """Минимальный контракт страницы Chromium, нужный для печати.

    Ровно то подмножество `playwright.async_api.Page`, которым пользуется
    `export_pdf`, — тесты подставляют вместо настоящей страницы простую
    заглушку с этими двумя методами.
    """

    async def set_content(self, html: str) -> None: ...

    async def pdf(self, **kwargs: object) -> bytes: ...


def _environment() -> Environment:
    """Своя Jinja-среда для печати: страница живёт вне контекста FastAPI."""
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    env.filters.update(TEMPLATE_FILTERS)
    return env


def render_print_html(view: ExportView) -> str:
    """Собрать печатный HTML отчёта: обложка, карточки, футер с подписью.

    Подпись эксперта показывается только при `not view.is_draft` — до
    валидации показ строки «проверено» был бы нарушением инварианта №2, даже
    если действующий вердикт уже частично проставлен.

    Источник карточек и строк сравнения — `view.client_report`, а не
    `view.report` (РЕШ-213): признак видимости для клиента (reject или пустой
    `evidence`) считает один раз `verdicts.visible_report`, эта функция
    своего фильтра по `evidence` не ведёт — БЛОК-6 финального аудита нашёл
    живой маршрут, где карточка без подтверждений не печаталась сама, но её
    строка в таблице сравнения оставалась, потому что решения принимались в
    двух разных местах. Красная плашка «нет подтверждений» для бессылочной
    карточки остаётся только на операторской странице отчёта (`report.html`,
    `_card.html`), которую эта функция не строит. Относительный порядок
    остальных карточек и разбиение на секции сохраняются — фильтр только
    убирает элементы, не переставляет их.

    Рекомендация и свободная проза оговорок (РЕШ-206) печатаются пополевым
    гейтом (РЕШ-211, `export.view.recommendation_printable`/`caveats_printable`):
    число карточек, реально попавших в печать (обе секции `client_report`),
    сравнивается с `view.total_cards` — числом карточек до всех фильтров.
    Эксперт мог поправить только одно из двух полей — второе при этом остаётся
    непровленным и печатается заглушкой отдельно от первого (БЛОК-5 финального
    аудита: общий гейт печатал непровленную рекомендацию, потому что эксперт
    правил только оговорки). Строка о недоступных источниках и машинные
    пометки (платный предел поиска, отказ интейка/рефайна/сводки) при этом
    остаются в любом случае (инвариант №5, РЕШ-210 — скрытый отказ источника
    или недобор поиска хуже видимого).

    Текст заглушки вместо рекомендации выбирается по факту, напечатана ли
    таблица сравнения в этом же документе (`bool(client_report.summary.
    comparison_rows)`), а не по причине её отсутствия (РЕШ-214): аудит нашёл
    два случая мимо `view.comparison_table_hidden` — сводка без строк
    сравнения при скрытой карточке и отчёт, где отклонены все карточки
    основной секции, — таблицы в обоих нет, а заглушка «по причине» всё равно
    отсылала клиента к разделу, которого в документе нет.

    `view.comparison_table_hidden` при этом остаётся — он решает, печатать ли
    на месте отсутствующей таблицы честную строку `COMPARISON_TABLE_HIDDEN_
    NOTICE` вместо тишины (РЕШ-212); это отдельное решение от текста заглушки
    у рекомендации.
    """
    report = view.client_report
    signature = ""
    if not view.is_draft and view.validated_at is not None:
        signature = signature_line(view.reviewer_name, view.validated_at)

    print_cards = report.cards
    print_contest_cards = report.contest_cards
    printed_total = len(print_cards) + len(print_contest_cards)
    recommendation_ok = recommendation_printable(view, printed_total)
    caveats_ok = caveats_printable(view, printed_total)
    table_printed = bool(report.summary.comparison_rows)
    recommendation_notice = SUMMARY_NOT_PRINTABLE_NOTICE if table_printed else SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE

    # `view.rework_keys` в шаблон не передаётся сознательно (РЕШ-198, контракт
    # `export/view.py`): пометка «на доработку» — подсказка оператору на
    # странице отчёта, в клиентские выгрузки (PDF, DOCX) она не попадает.
    template = _environment().get_template("report_print.html")
    return template.render(
        report=report,
        print_cards=print_cards,
        print_contest_cards=print_contest_cards,
        is_draft=view.is_draft,
        signature=signature,
        print_css=_PRINT_CSS_PATH.read_text(encoding="utf-8"),
        maturity_ru=MATURITY_RU,
        confidence_ru=CONFIDENCE_RU,
        contest_heading=CONTEST_SECTION_HEADING.removeprefix("## "),
        recommendation_printable=recommendation_ok,
        recommendation_notice=recommendation_notice,
        comparison_table_hidden=view.comparison_table_hidden,
        comparison_hidden_notice=COMPARISON_TABLE_HIDDEN_NOTICE,
        comparison_headers_ru=COMPARISON_HEADERS_RU,
        limitation_lines=limitations_lines(report, include_caveats=caveats_ok),
    )


async def export_pdf(view: ExportView, out_path: Path, *, page: PrintPage | None = None) -> Path:
    """Отрендерить отчёт и сохранить его как PDF по указанному пути.

    `page`, переданная явно, полностью заменяет запуск Chromium — этим
    пользуются тесты (РЕШ-196 п. 12). В боевом вызове (команда `scout export`,
    задача Э4-И6) параметр не передаётся, и функция сама поднимает и
    закрывает браузер группы `export`.
    """
    html = render_print_html(view)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if page is not None:
        await page.set_content(html)
        pdf_bytes = await page.pdf(format="A4", print_background=True)
        # Запись файла — через поток: `Path.write_bytes` синхронный, прямой
        # вызов в корутине блокировал бы цикл событий (ASYNC240).
        await asyncio.to_thread(out_path.write_bytes, pdf_bytes)
        return out_path

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            browser_page = await browser.new_page()
            await browser_page.set_content(html)
            pdf_bytes = await browser_page.pdf(format="A4", print_background=True)
            await asyncio.to_thread(out_path.write_bytes, pdf_bytes)
        finally:
            await browser.close()
    return out_path
