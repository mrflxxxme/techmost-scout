"""Стенд-тесты пятой проверки аудита Э4-П15 (П-23, П-25) через настоящий
`build_export_view` — не в `tests/test_summary_field_gate.py`, чтобы не
раздувать его сверх 500 строк (страж `tests/test_tests_layout.py`); свой
временный файл SQLite, сеть не задействована (инвариант №7). Фикстуры не
переиспользуются из `test_summary_field_gate.py` намеренно, тем же доводом,
что и в докстроке её `db_session_factory`: дублирование дешевле связывания.

П-23: мутация «`export.view._comparison_table_hidden` считает признак с
`require_evidence=False`» переживала весь набор — все прежние сценарии
несовпадения строк прятали таблицу через `REJECT`, а под `require_evidence=
False` `hidden_reason` реагирует на `REJECT` так же, как и под `True`. Нужен
сценарий, где карточка скрыта **только** пустым `evidence`, без единого
`REJECT`.

П-25: `export.view._reindex_after_filter` раньше индексировала
`edited_report.cards[key[1]]` ради аргумента, который вызов с
`require_evidence=False` (единственный, что здесь есть) не читает вовсе, —
при рассинхроне «строк `solution_cards` в базе больше, чем карточек в
`report_json`» это падало `IndexError` (500 на операторской странице отчёта и
на выгрузке).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export.markdown import COMPARISON_TABLE_HIDDEN_NOTICE
from app.export.pdf import render_print_html
from app.export.view import ExportView, build_export_view
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.schemas import Report, ReportSummary, SolutionCard


def _make_card(**overrides) -> SolutionCard:
    fields = {
        "title": "Регенерация катализатора добавками молибдена",
        "essence": "Продление срока службы катализатора за счёт присадки.",
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "next_step": "Запросить образец у разработчика.",
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _make_report(**overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": [_make_card()],
        "contest_cards": [],
        "summary": ReportSummary(recommendation="Рекомендация."),
        "generated_at": datetime(2026, 8, 27, 9, 0),
    }
    fields.update(overrides)
    return Report(**fields)


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite (форма `app.db.session_scope`)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'export-view-p15-test.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    yield factory
    await engine.dispose()


async def _seed_request(session_factory, *, report: Report) -> tuple[int, dict[tuple[bool, int], int]]:
    """Строка `requests` с готовым `report_json` и строками `solution_cards`."""
    async with session_factory() as session:
        row = RequestRow(
            raw_text="катализатор теряет активность раньше срока",
            status=RequestStatus.DRAFTED,
            report_json=report.model_dump_json(),
        )
        session.add(row)
        await session.flush()
        request_id = int(row.id)
        card_rows: dict[tuple[bool, int], SolutionCardRow] = {}

        def _add_section(cards: list[SolutionCard], *, is_contest: bool) -> None:
            for position, card in enumerate(cards):
                card_row = SolutionCardRow(
                    request_id=request_id,
                    position=position,
                    is_contest=is_contest,
                    title=card.title,
                    essence=card.essence,
                    maturity_stage=card.maturity_stage.value,
                    applicability=card.applicability,
                    next_step=card.next_step,
                    confidence=card.confidence.value,
                )
                session.add(card_row)
                card_rows[(is_contest, position)] = card_row

        _add_section(report.cards, is_contest=False)
        _add_section(report.contest_cards, is_contest=True)
        await session.flush()
        card_ids = {key: int(card_row.id) for key, card_row in card_rows.items()}
    return request_id, card_ids


async def _build_view(session_factory, request_id: int) -> ExportView:
    async with session_factory() as session:
        return await build_export_view(session, request_id)


def test_comparison_table_hidden_карточка_скрыта_только_пустым_evidence(db_session_factory, brief):
    """П-23: карточка скрыта **только** пустым `evidence` (оператор снял
    единственное подтверждение через `POST /cards/1/edit`, здесь — карточка
    изначально без `evidence`, тот же итог, что и в БЛОК-6-тесте
    `tests/test_summary_field_gate.py`) — без единого `REJECT`. Три карточки,
    две строки сравнения (несовпадение: строк меньше, чем карточек).

    На настоящем коде (`require_evidence=True`) бессылочная карточка учтена
    как скрытая, несовпадение обнаружено — `comparison_table_hidden=True`, и
    в PDF на месте таблицы печатается честная строка
    `COMPARISON_TABLE_HIDDEN_NOTICE`. На мутации (`require_evidence=False`)
    `hidden_reason` бессылочную карточку скрытой не считает — видимых
    карточек остаётся столько же, сколько было, охрана «скрывать нечего»
    срабатывает раньше времени, и раздел «Сравнение вариантов» исчезает
    молча."""
    bare = _make_card(title="Присадка без подтверждений", evidence=[])
    kept_1 = _make_card(title="Испытанный вариант 1")
    kept_2 = _make_card(title="Испытанный вариант 2")
    report = _make_report(
        brief=brief,
        cards=[bare, kept_1, kept_2],
        summary=ReportSummary(
            recommendation="Рекомендация.",
            comparison_rows=[{"option": "А"}, {"option": "Б"}],  # 2 строки на 3 карточки
        ),
    )
    request_id, _card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    # Ни одного вердикта не выставляем — карточка скрыта только пустым evidence.

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.comparison_table_hidden is True
    assert view.client_report.summary.comparison_rows == []

    html = render_print_html(view)
    assert COMPARISON_TABLE_HIDDEN_NOTICE in html
    assert "<h2>Сравнение вариантов</h2>" in html  # раздел на месте, не исчез молча


def test_reindex_after_filter_переживает_рассинхрон_строк_с_report_json(db_session_factory, brief):
    """П-25: строк `solution_cards` в базе больше, чем карточек в
    `report_json` (продуктовыми путями недостижимо — например, повторный
    прогон пайплайна дописал строку карточки, а `report_json` не
    переписался), не роняет `build_export_view` `IndexError`.

    До правки `_reindex_after_filter` индексировала `edited_report.cards[key[1]]`
    только затем, чтобы передать значение, которое `hidden_reason` при
    `require_evidence=False` не читает вовсе, — вторая строка `solution_cards`
    (позиция 1) не находила пары в `report.cards` (там всего одна карточка) и
    падала `IndexError`. Версия до четвёртой волны индексацию не вела и
    результат отдавала."""
    report = _make_report(brief=brief, cards=[_make_card(title="Единственная в report_json")])
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))

    async def _add_desync_row() -> int:
        async with db_session_factory() as session:
            row = SolutionCardRow(
                request_id=request_id,
                position=1,
                is_contest=False,
                title="Лишняя строка вне report_json",
                essence="Появилась в базе без парной карточки в отчёте.",
                maturity_stage="unknown",
                applicability="—",
                next_step="—",
                confidence="medium",
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    extra_id = asyncio.run(_add_desync_row())

    view = asyncio.run(_build_view(db_session_factory, request_id))

    # Операторская страница (`view.report`) строится из `edited_report.cards` —
    # лишней строки базы там просто нет, она видит ровно одну карточку.
    assert [c.title for c in view.report.cards] == ["Единственная в report_json"]
    # `_reindex_after_filter` считает позиции по строкам базы (`original_ids`),
    # а не по `edited_report.cards` — решение «скрыта ли» здесь никогда не было
    # про существование карточки в отчёте, только про её вердикт. Лишняя строка
    # без вердикта не скрыта и получает свою позицию, хотя показывать её не
    # для чего, — не идеальный результат при этом невозможном на практике
    # рассинхроне, но тот же результат, что отдавала версия кода до четвёртой
    # волны: она эту секцию тоже считала только по `original_ids`/`effective`,
    # не заглядывая в список карточек вовсе. Не упасть здесь важнее, чем
    # угадать.
    assert view.card_ids == {
        (False, 0): card_ids[(False, 0)],
        (False, 1): extra_id,
    }
    assert view.rework_keys == set()
