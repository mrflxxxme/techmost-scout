"""Пополевые ворота печати сводки в DOCX (РЕШ-211, РЕШ-212).

Новый файл — `tests/test_export_docx.py` уже ровно на пределе 500 строк
(страж `tests/test_tests_layout.py`), новое поведение получает отдельное
место. Сеть не задействована (инвариант №7), только `tmp_path`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from docx import Document

from app.export.docx import export_docx
from app.export.markdown import (
    COMPARISON_TABLE_HIDDEN_NOTICE,
    SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE,
    SUMMARY_NOT_PRINTABLE_NOTICE,
)
from app.export.view import ExportView
from app.schemas import Evidence, Report, ReportSummary, SolutionCard


def _make_card(**overrides) -> SolutionCard:
    fields = {
        "title": "Регенерация катализатора добавками молибдена",
        "essence": "Продление срока службы катализатора за счёт присадки.",
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "next_step": "Запросить образец у разработчика.",
        "evidence": [Evidence(claim="Испытан на действующей установке", url="https://example.org/proof")],
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _make_report(cards: list[SolutionCard], brief, **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": brief,
        "cards": cards,
        "contest_cards": [],
        "summary": ReportSummary(recommendation="Начать с варианта №1."),
        "generated_at": datetime(2026, 8, 27, 9, 0),
    }
    fields.update(overrides)
    return Report(**fields)


def _make_view(report: Report, *, total_cards: int | None = None, **overrides) -> ExportView:
    fields = {
        "request_id": report.request_id,
        "report": report,
        # По умолчанию клиентский вид совпадает с операторским — эти тесты не
        # проверяют фильтр по `evidence` (он в `tests/test_verdicts.py` и в
        # БЛОК-6-тесте ниже), у карточек `_make_card()` подтверждение уже есть.
        "client_report": overrides.pop("client_report", report),
        "card_ids": {},
        "is_draft": True,
        "reviewer_name": "",
        "validated_at": None,
        "total_cards": total_cards if total_cards is not None else len(report.cards) + len(report.contest_cards),
        "recommendation_edited": False,
        "caveats_edited": False,
        "comparison_table_hidden": False,
    }
    fields.update(overrides)
    return ExportView(**fields)


def _full_text(path: Path) -> str:
    document = Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def test_правка_только_оговорок_печатает_оговорки_не_рекомендацию(tmp_path: Path, brief):
    """Критерий 2: эксперт правил только оговорки — рекомендация не печатается.
    Таблица сравнения напечатана (строка на карточку) — заглушка вместо
    рекомендации вправе на неё ссылаться (РЕШ-214, факт «таблица напечатана»)."""
    report = _make_report(
        [_make_card()],
        brief,
        summary=ReportSummary(
            recommendation="Первым берём вариант 1.",
            caveats=["Оговорка эксперта: проверил лично."],
            comparison_rows=[{"option": "Вариант 1"}],
        ),
    )
    view = _make_view(report, total_cards=2, caveats_edited=True)
    out_path = tmp_path / "a.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "Первым берём вариант 1." not in text
    assert "Оговорка эксперта: проверил лично." in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE in text


def test_правка_только_рекомендации_печатает_рекомендацию_не_оговорки(tmp_path: Path, brief):
    """Зеркало критерия 2: правлена только рекомендация — оговорки скрыты."""
    report = _make_report(
        [_make_card()],
        brief,
        summary=ReportSummary(
            recommendation="Начать со второго варианта.", caveats=["В карточке 3 риск не подтверждён."]
        ),
    )
    view = _make_view(report, total_cards=2, recommendation_edited=True)
    out_path = tmp_path / "b.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "Начать со второго варианта." in text
    assert "В карточке 3 риск не подтверждён." not in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in text


def test_comparison_table_hidden_печатает_честную_строку(tmp_path: Path, brief):
    """РЕШ-212: таблица спрятана — на её месте честная строка вместо тишины."""
    report = _make_report([_make_card()], brief, summary=ReportSummary(recommendation="x", comparison_rows=[]))
    view = _make_view(report, total_cards=2, comparison_table_hidden=True)
    out_path = tmp_path / "c.docx"

    export_docx(view, out_path)

    document = Document(str(out_path))
    assert not document.tables
    text = _full_text(out_path)
    assert COMPARISON_TABLE_HIDDEN_NOTICE in text


def test_comparison_table_hidden_печатает_заголовок_раздела(tmp_path: Path, brief):
    """П-18 аудита, мутация 3 (`_add_comparison_table`): у спрятанной таблицы
    честная строка `COMPARISON_TABLE_HIDDEN_NOTICE` печатается под тем же
    заголовком раздела «Сравнение вариантов», что и у обычной таблицы —
    сегодня заголовок в этом случае проверяется только текстом строки, без
    самого заголовка."""
    report = _make_report([_make_card()], brief, summary=ReportSummary(recommendation="x", comparison_rows=[]))
    view = _make_view(report, total_cards=2, comparison_table_hidden=True)
    out_path = tmp_path / "i.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "Сравнение вариантов" in text


def test_comparison_table_hidden_заглушка_не_ссылается_на_таблицу(tmp_path: Path, brief):
    """П-14 финального аудита: заглушка вместо рекомендации не отсылает
    клиента к разделу, которого в документе нет."""
    report = _make_report(
        [_make_card()], brief, summary=ReportSummary(recommendation="Первым берём вариант 1.", comparison_rows=[])
    )
    view = _make_view(report, total_cards=2, comparison_table_hidden=True)
    out_path = tmp_path / "d.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in text
    assert "Ориентируйтесь на таблицу сравнения" not in text


def test_comparison_table_not_hidden_заглушка_ссылается_на_таблицу_как_раньше(tmp_path: Path, brief):
    """Обратная сторона: таблица напечатана — прежняя формулировка остаётся."""
    report = _make_report(
        [_make_card()],
        brief,
        summary=ReportSummary(recommendation="Первым берём вариант 1.", comparison_rows=[{"option": "Вариант 1"}]),
    )
    view = _make_view(report, total_cards=2, comparison_table_hidden=False)
    out_path = tmp_path / "e.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert SUMMARY_NOT_PRINTABLE_NOTICE in text
    assert SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE not in text
    assert COMPARISON_TABLE_HIDDEN_NOTICE not in text


def test_comparison_table_not_hidden_и_строк_нет_секция_не_печатается(tmp_path: Path, brief):
    """Пустая сводка модели с самого начала — раздел не печатается вовсе."""
    report = _make_report([_make_card()], brief, summary=ReportSummary(recommendation="x", comparison_rows=[]))
    view = _make_view(report)
    out_path = tmp_path / "f.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "Сравнение вариантов" not in text
    assert COMPARISON_TABLE_HIDDEN_NOTICE not in text


def test_реш214_сводка_без_строк_при_скрытой_карточке_не_ссылается_на_таблицу(tmp_path: Path, brief):
    """РЕШ-214, случай аудита 1: строк сравнения не было с самого начала,
    карточка скрыта (`total_cards` больше печатаемых), `comparison_table_
    hidden=False` (так его и вычисляет `_comparison_table_hidden`, когда
    исходных строк не было). Заглушка обязана смотреть на факт «таблица
    напечатана», а не на этот флаг."""
    report = _make_report(
        [_make_card()], brief, summary=ReportSummary(recommendation="Первым берём вариант 1.", comparison_rows=[])
    )
    view = _make_view(report, total_cards=2, comparison_table_hidden=False)
    out_path = tmp_path / "g.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in text
    assert "Ориентируйтесь на таблицу сравнения" not in text


def test_реш214_все_карточки_основной_секции_отклонены_конкурсная_не_ссылается_на_таблицу(tmp_path: Path, brief):
    """РЕШ-214, случай аудита 2: все карточки основной секции скрыты
    (`client_report.cards` пуст), конкурсная секция осталась — строки
    сравнения отфильтрованы до пустого списка, но `comparison_table_hidden`
    остаётся `False` (число исходных строк совпало с числом карточек до
    фильтра — ветка «несовпадение» не сработала). Таблицы в документе нет,
    заглушка не вправе на неё ссылаться."""
    contest_card = _make_card(title="Конкурсная карточка")
    client_report = _make_report([], brief, contest_cards=[contest_card], summary=ReportSummary(recommendation="x"))
    report = _make_report(
        [_make_card(title="Отклонённая 1"), _make_card(title="Отклонённая 2")],
        brief,
        contest_cards=[contest_card],
        summary=ReportSummary(recommendation="Первым берём вариант 1."),
    )
    view = _make_view(report, client_report=client_report, total_cards=4, comparison_table_hidden=False)
    out_path = tmp_path / "h.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in text
    assert "Ориентируйтесь на таблицу сравнения" not in text
