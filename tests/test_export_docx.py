"""Проверки выгрузки отчёта в DOCX (Э4-И5, SPEC Э4-2 часть 2).

`export_docx` принимает уже готовый `ExportView` — тем же контрактом, что и
страница отчёта, поэтому в тестах он собирается напрямую, без базы данных
(инвариант №7: сети и файловой БД тест не требует, только `tmp_path`). Тесты
РЕШ-211/212 — в `test_export_docx_field_gate.py` (предел 500 строк).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from docx import Document

from app.export.docx import DRAFT_NOTICE, export_docx
from app.export.markdown import PAID_LIMIT_NOTICE, REVIEWED_FOOTER, SUMMARY_NOT_PRINTABLE_NOTICE
from app.export.view import ExportView
from app.schemas import (
    Evidence,
    GovSupportMatch,
    MaturityStage,
    Report,
    ReportSummary,
    RequirementCheck,
    SolutionCard,
)


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


def _make_report(cards: list[SolutionCard], contest_cards: list[SolutionCard] | None = None, **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": cards,
        "contest_cards": contest_cards or [],
        "summary": ReportSummary(recommendation="Начать с варианта №1."),
        "generated_at": datetime(2026, 8, 27, 9, 0),
    }
    fields.update(overrides)
    return Report(**fields)


def _make_view(
    report: Report,
    *,
    is_draft: bool = True,
    reviewer_name: str = "",
    validated_at=None,
    total_cards: int | None = None,
    recommendation_edited: bool = False,
    caveats_edited: bool = False,
) -> ExportView:
    # По умолчанию ничего не скрыто (РЕШ-206): совпадает с числом карточек отчёта.
    if total_cards is None:
        total_cards = len(report.cards) + len(report.contest_cards)
    return ExportView(
        request_id=report.request_id,
        report=report,
        card_ids={},
        is_draft=is_draft,
        reviewer_name=reviewer_name,
        validated_at=validated_at,
        total_cards=total_cards,
        recommendation_edited=recommendation_edited,
        caveats_edited=caveats_edited,
    )


def _full_text(path: Path) -> str:
    """Весь текст документа одной строкой — абзацы и ячейки таблиц."""
    document = Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def test_файл_создаётся_с_именем_выгрузки_и_читается_обратно(tmp_path: Path, brief):
    """Критерий 1: имя `report-<id>-<дата>.docx`, файл открывается python-docx,
    в тексте есть заголовок карточки и URL подтверждения."""
    card = _make_card(title="Катализатор с молибденовой присадкой")
    report = _make_report([card], brief=brief)
    view = _make_view(report, is_draft=True)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    result = export_docx(view, out_path)

    assert result == out_path
    assert out_path.exists()
    text = _full_text(out_path)
    assert "Катализатор с молибденовой присадкой" in text
    assert "https://example.org/proof" in text


def test_черновик_есть_пометка_и_нет_проверено(tmp_path: Path, brief):
    """Критерий 2 (часть 1): `is_draft` → есть DRAFT_NOTICE, нет «проверено:»."""
    report = _make_report([_make_card()], brief=brief)
    view = _make_view(report, is_draft=True)

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    text = _full_text(tmp_path / "report-1-2026-08-27.docx")
    assert DRAFT_NOTICE in text
    assert "проверено:" not in text


def test_validated_ровно_подпись_и_ни_одного_черновика(tmp_path: Path, brief):
    """Критерий 2 (часть 2): при `validated` — ровно строка РЕШ-197, ни одного
    вхождения «ЧЕРНОВИК»."""
    report = _make_report([_make_card()], brief=brief)
    when = datetime(2026, 8, 27)
    view = _make_view(report, is_draft=False, reviewer_name="Иван Иванов", validated_at=when)

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    text = _full_text(tmp_path / "report-1-2026-08-27.docx")
    expected = REVIEWED_FOOTER.format(name="Иван Иванов", date="27.08.2026")
    assert expected in text
    assert "ЧЕРНОВИК" not in text


def test_карточка_без_evidence_не_попадает_в_docx(tmp_path: Path, brief):
    """РЕШ-204: карточка без единого подтверждения не печатается вовсе."""
    bare = _make_card(title="Карточка без подтверждений", maturity_stage=MaturityStage.LAB, trl_estimate=5, evidence=[])
    report = _make_report([bare], brief=brief)
    view = _make_view(report, is_draft=True)

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    text = _full_text(tmp_path / "report-1-2026-08-27.docx")
    assert "Карточка без подтверждений" not in text
    assert "Стадия зрелости" not in text
    assert "TRL" not in text
    assert "Пригодных вариантов не найдено." in text


def test_карточка_с_evidence_печатает_стадию_и_trl(tmp_path: Path, brief):
    """Регрессия к инварианту №1: подтверждённая карточка стадию всё же печатает."""
    card = _make_card(
        maturity_stage=MaturityStage.PILOT,
        trl_estimate=6,
        evidence=[Evidence(claim="Пилот на объекте", url="https://example.org/pilot")],
    )
    report = _make_report([card], brief=brief)
    view = _make_view(report, is_draft=True)

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    text = _full_text(tmp_path / "report-1-2026-08-27.docx")
    assert "прошла пилот на объекте" in text
    assert "TRL ≈ 6" in text


def test_карточка_без_evidence_пропускается_а_порядок_остальных_сохраняется(tmp_path: Path, brief):
    """Смешанный список: карточка без evidence выпадает, соседние сохраняют порядок."""
    first = _make_card(title="Первая с подтверждением")
    bare = _make_card(title="Вторая без подтверждения", evidence=[])
    third = _make_card(title="Третья с подтверждением")
    report = _make_report([first, bare, third], brief=brief)
    view = _make_view(report, is_draft=True)

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    text = _full_text(tmp_path / "report-1-2026-08-27.docx")
    assert "Вторая без подтверждения" not in text
    assert text.index("Первая с подтверждением") < text.index("Третья с подтверждением")


def test_конкурсная_карточка_без_evidence_не_попадает_и_секция_скрывается(tmp_path: Path, brief):
    """Если у всех конкурсных карточек пустой evidence — заголовок секции не печатается."""
    main_card = _make_card(title="Основная карточка")
    bare_contest = _make_card(title="Конкурсная без подтверждения", evidence=[])
    report = _make_report([main_card], contest_cards=[bare_contest], brief=brief)
    view = _make_view(report, is_draft=True)

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    text = _full_text(tmp_path / "report-1-2026-08-27.docx")
    assert "Конкурсная без подтверждения" not in text
    assert "Ранние разработки и команды" not in text


def test_секция_сравнения_печатается_таблицей(tmp_path: Path, brief):
    """Сравнение вариантов идёт таблицей: заголовки и значения строк видны."""
    report = _make_report(
        [_make_card()],
        brief=brief,
        summary=ReportSummary(
            recommendation="Начать с варианта №1.",
            comparison_rows=[{"Вариант": "Молибден", "Срок": "24 месяца"}],
        ),
    )
    view = _make_view(report, is_draft=True)

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    document = Document(str(tmp_path / "report-1-2026-08-27.docx"))
    assert len(document.tables) == 1
    header = [cell.text for cell in document.tables[0].rows[0].cells]
    assert header == ["Вариант", "Срок"]


def test_конкурсная_секция_и_разработчик_печатаются(tmp_path: Path, brief):
    """Секция «Ранние разработки и команды» печатается при непустом
    `contest_cards`; `developer_line` не тянет хвост «— не найдено»."""
    main_card = _make_card(title="Основная карточка")
    contest_card = _make_card(
        title="Конкурсная карточка",
        developer_org="ООО «Ромашка»",
        developer_team="не найдено",
    )
    report = _make_report([main_card], contest_cards=[contest_card], brief=brief)
    view = _make_view(report, is_draft=True)

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    text = _full_text(tmp_path / "report-1-2026-08-27.docx")
    assert "Ранние разработки и команды" in text
    assert "Конкурсная карточка" in text
    assert "ООО «Ромашка» — не найдено" not in text


def test_ограничения_и_риски_и_господдержка_печатаются(tmp_path: Path, brief):
    """Раздел ограничений, риски, господдержка и построчная сверка требований —
    все опциональные блоки карточки отражаются в тексте документа."""
    card = _make_card(
        risks=["Долгий цикл поставки присадки"],
        gov_support=[GovSupportMatch(instrument="Грант ФСИ", why="Подходит по отрасли")],
        requirement_checks=[RequirementCheck(requirement="Работа при 340 °C", status="confirmed")],
    )
    report = _make_report(
        [card],
        brief=brief,
        sources_unavailable=["cyberleninka"],
    )
    view = _make_view(report, is_draft=True)

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    text = _full_text(tmp_path / "report-1-2026-08-27.docx")
    assert "Ограничения этого поиска" in text
    assert "Не ответили источники" in text
    assert "Долгий цикл поставки присадки" in text
    assert "Грант ФСИ" in text
    assert "Работа при 340 °C" in text


def test_создаёт_недостающий_каталог(tmp_path: Path, brief):
    """`out_path` в несуществующем подкаталоге — каталог создаётся."""
    report = _make_report([_make_card()], brief=brief)
    view = _make_view(report, is_draft=True)
    out_path = tmp_path / "vault" / "report-1-2026-08-27.docx"

    result = export_docx(view, out_path)

    assert result.exists()


def _header_text(path: Path) -> str:
    """Текст первого абзаца колонтитула первой (единственной) секции."""
    document = Document(str(path))
    header = document.sections[0].header
    return header.paragraphs[0].text if header.paragraphs else ""


def test_черновик_с_вердиктом_не_печатает_подпись(tmp_path: Path, brief):
    """П-1 аудита: мутация «печатать подпись без проверки is_draft» убита —
    даже при заполненном `validated_at` черновик подписи не печатает."""
    report = _make_report([_make_card()], brief=brief)
    view = _make_view(report, is_draft=True, reviewer_name="Иван Иванов", validated_at=datetime(2026, 8, 27))

    export_docx(view, tmp_path / "report-1-2026-08-27.docx")

    text = _full_text(tmp_path / "report-1-2026-08-27.docx")
    assert "проверено:" not in text
    assert DRAFT_NOTICE in text


def test_черновик_печатает_пометку_в_колонтитуле(tmp_path: Path, brief):
    """П-4 аудита: пометка черновика видна на каждой странице — в колонтитуле."""
    report = _make_report([_make_card()], brief=brief)
    view = _make_view(report, is_draft=True)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    assert DRAFT_NOTICE in _header_text(out_path)


def test_проверенный_документ_не_имеет_пометки_в_колонтитуле(tmp_path: Path, brief):
    """У проверенного документа колонтитула с пометкой черновика быть не должно."""
    report = _make_report([_make_card()], brief=brief)
    view = _make_view(report, is_draft=False, reviewer_name="Иван Иванов", validated_at=datetime(2026, 8, 27))
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    assert DRAFT_NOTICE not in _header_text(out_path)


def test_рекомендация_не_печатается_когда_карточка_скрыта_и_правки_нет(tmp_path: Path, brief):
    """РЕШ-206: карточка скрыта, правки нет — рекомендация не печатается, есть заглушка."""
    report = _make_report(
        [_make_card()],
        brief=brief,
        summary=ReportSummary(recommendation="Первым берём вариант 1.", caveats=["В карточке 3 риск не подтверждён."]),
    )
    view = _make_view(report, is_draft=True, total_cards=2, recommendation_edited=False, caveats_edited=False)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "Первым берём вариант 1." not in text
    assert "В карточке 3 риск не подтверждён." not in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE in text


def test_рекомендация_не_печатается_когда_карточка_скрыта_пустым_evidence(tmp_path: Path, brief):
    """РЕШ-206, регрессия ревью цикла 2: total_cards совпадает с числом карточек
    в отчёте (reject-вердикта нет вовсе), но одна карточка скрывается самим
    экспортёром из-за пустого evidence (РЕШ-204) — рекомендация и оговорки всё
    равно не печатаются. Мутация «считать printed_cards по нефильтрованным
    report.cards/contest_cards» сделала бы printed_cards == total_cards и
    напечатала бы текст модели — эта проверка её убивает."""
    bare = _make_card(title="Карточка без подтверждений", evidence=[])
    report = _make_report(
        [_make_card(), bare],
        brief=brief,
        summary=ReportSummary(recommendation="Первым берём вариант 1.", caveats=["В карточке 3 риск не подтверждён."]),
    )
    view = _make_view(report, is_draft=True, total_cards=2, recommendation_edited=False, caveats_edited=False)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "Первым берём вариант 1." not in text
    assert "В карточке 3 риск не подтверждён." not in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE in text


def test_строка_недоступных_источников_остаётся_при_скрытой_сводке(tmp_path: Path, brief):
    """Инвариант №5: строка о недоступных источниках остаётся, даже когда сводка скрыта."""
    report = _make_report(
        [_make_card()],
        brief=brief,
        summary=ReportSummary(recommendation="Первым берём вариант 1.", caveats=["Оговорка модели."]),
        sources_unavailable=["cyberleninka"],
    )
    view = _make_view(report, is_draft=True, total_cards=2, recommendation_edited=False, caveats_edited=False)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "Не ответили источники" in text
    assert "Оговорка модели." not in text


def test_рекомендация_печатается_целиком_когда_эксперт_правил_сводку(tmp_path: Path, brief):
    """РЕШ-206 (зеркало): карточка скрыта, но эксперт правил сводку —
    печатается его текст целиком, включая оговорки."""
    report = _make_report(
        [_make_card()],
        brief=brief,
        summary=ReportSummary(recommendation="Текст эксперта.", caveats=["Правленая оговорка."]),
    )
    view = _make_view(report, is_draft=True, total_cards=2, recommendation_edited=True, caveats_edited=True)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "Текст эксперта." in text
    assert "Правленая оговорка." in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in text


def test_шапка_таблицы_сравнения_печатается_русскими_заголовками(tmp_path: Path, brief):
    """РЕШ-207: латинские ключи строк сравнения печатаются русскими заголовками."""
    report = _make_report(
        [_make_card()],
        brief=brief,
        summary=ReportSummary(
            recommendation="Начать с варианта №1.",
            comparison_rows=[
                {"option": "Молибден", "maturity": "pilot", "gives": "испытан", "needs_from_client": "образец"}
            ],
        ),
    )
    view = _make_view(report, is_draft=True)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    document = Document(str(out_path))
    header = [cell.text for cell in document.tables[0].rows[0].cells]
    assert header == ["Вариант", "Стадия зрелости", "Что подтверждено", "Что нужно от вас"]


def test_в_docx_нет_строки_о_стоимости(tmp_path: Path, brief):
    """РЕШ-207: стоимости машинной обработки в клиентском DOCX нет."""
    report = _make_report([_make_card()], brief=brief, cost_rub=11.3711)
    view = _make_view(report, is_draft=True)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "стоимость" not in text
    assert "₽" not in text


def test_сквозной_отчёт_с_отклонённой_карточкой_без_правки_сводки(tmp_path: Path, brief):
    """Сквозной сценарий: отчёт из 4 карточек, одна отклонена экспертом
    (`build_export_view` её уже убрал бы) — в выгрузке 3 карточки, 3 строки
    сравнения, текста рекомендации нет, оговорок нет, строка о недоступных
    источниках на месте, стоимости нет, шапка таблицы русская."""
    visible_cards = [_make_card(title=f"Карточка №{n}") for n in (1, 2, 3)]
    report = _make_report(
        visible_cards,
        brief=brief,
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
    view = _make_view(report, is_draft=True, total_cards=4, recommendation_edited=False, caveats_edited=False)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    document = Document(str(out_path))
    text = _full_text(out_path)
    assert text.count("Карточка №") == 3
    assert len(document.tables[0].rows) == 4  # заголовок + 3 строки сравнения
    header = [cell.text for cell in document.tables[0].rows[0].cells]
    assert header == ["Вариант", "Стадия зрелости", "Что подтверждено", "Что нужно от вас"]
    assert "Первым берём вариант 1." not in text
    assert "В карточке 3 риск не подтверждён." not in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE in text
    assert "Не ответили источники" in text
    assert "стоимость" not in text
    assert "₽" not in text


def test_сквозной_отчёт_с_отклонённой_карточкой_эксперт_правил_сводку(tmp_path: Path, brief):
    """Зеркальный сценарий: та же ситуация, но эксперт правил сводку —
    печатается его текст целиком."""
    visible_cards = [_make_card(title=f"Карточка №{n}") for n in (1, 2, 3)]
    report = _make_report(
        visible_cards,
        brief=brief,
        summary=ReportSummary(
            recommendation="Текст, который правил эксперт.",
            caveats=["Оговорка, которую эксперт оставил."],
            comparison_rows=[{"option": "Вариант 1"}, {"option": "Вариант 2"}, {"option": "Вариант 3"}],
        ),
    )
    view = _make_view(report, is_draft=True, total_cards=4, recommendation_edited=True, caveats_edited=True)
    out_path = tmp_path / "report-1-2026-08-27.docx"

    export_docx(view, out_path)

    text = _full_text(out_path)
    assert "Текст, который правил эксперт." in text
    assert "Оговорка, которую эксперт оставил." in text
    assert SUMMARY_NOT_PRINTABLE_NOTICE not in text


def test_машинная_пометка_и_учёт_конкурсных_карточек(tmp_path: Path, brief):
    """Критерии 1, 3 (РЕШ-210) и 4 (П-10), в одном тесте ради предела 500 строк."""
    view = lambda **kw: _make_view(_make_report([_make_card()], brief=brief, **kw), total_cards=2)  # noqa: E731
    export_docx(view(summary=ReportSummary(recommendation="x", caveats=[PAID_LIMIT_NOTICE, "проза"])), tmp_path / "a")
    text = _full_text(tmp_path / "a")
    assert "Ограничения этого поиска" in text and PAID_LIMIT_NOTICE in text and "проза" not in text
    rec = "Рекомендация-42"
    export_docx(view(contest_cards=[_make_card(title="К")], summary=ReportSummary(recommendation=rec)), tmp_path / "b")
    assert rec in _full_text(tmp_path / "b")
