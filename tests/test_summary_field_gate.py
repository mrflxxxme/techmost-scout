"""Стенд-тесты пополевых ворот печати сводки через настоящий `build_export_view`
(БЛОК-5 финального аудита Э4, РЕШ-211, РЕШ-212).

Вынесены из `tests/test_summary_overrides.py`, чтобы не растить его сверх
500 строк (страж `tests/test_tests_layout.py`) — свой стенд БД на временном
файле SQLite, сеть не задействована (инвариант №7).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export.markdown import SUMMARY_NOT_PRINTABLE_NOTICE
from app.export.pdf import render_print_html
from app.export.view import ExportView, build_export_view, caveats_printable, recommendation_printable
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import SummaryOverride as SummaryOverrideRow
from app.models import Verdict as VerdictRow
from app.schemas import Report, ReportSummary, SolutionCard
from app.schemas import Verdict as VerdictDecision


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
        "summary": ReportSummary(
            recommendation="Первым берём вариант 1.",
            caveats=["В карточке 3 нет данных о TRL."],
        ),
        "generated_at": datetime(2026, 8, 27, 9, 0),
    }
    fields.update(overrides)
    return Report(**fields)


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite (тот же приём, что и в
    `test_summary_overrides.py` — второй фикстуры для той же формы нет
    смысла заводить общей: файлы независимы, дублирование дешевле связывания)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'summary-field-gate-test.db'}", future=True)
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


async def _add_verdict(session_factory, card_id: int, decision: VerdictDecision) -> None:
    async with session_factory() as session:
        session.add(VerdictRow(card_id=card_id, reviewer_name="Иван Иванов", decision=decision.value))


async def _build_view(session_factory, request_id: int) -> ExportView:
    async with session_factory() as session:
        return await build_export_view(session, request_id)


def test_правка_только_оговорок_печатает_независимо_блок5(db_session_factory, brief):
    """Критерий 1, воспроизведение БЛОК-5 финального аудита стендом через
    настоящий `build_export_view`: две карточки, карточка 1 отклонена, эксперт
    правит только оговорки. `recommendation_edited` обязан остаться `False`
    (система честно помнит «не правил»), а нетронутая рекомендация модели —
    «Первым берём вариант 1 — «Первый».» — не должна попасть ни в
    `recommendation_printable`, ни в реальный печатный HTML, хотя карточки
    «Первый» в документе уже нет."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title="Первый"), _make_card(title="Второй")],
        summary=ReportSummary(
            recommendation="Первым берём вариант 1 — «Первый».",
            caveats=["Оговорка модели про карточку 2."],
        ),
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    async def _seed_override() -> None:
        async with db_session_factory() as session:
            session.add(SummaryOverrideRow(request_id=request_id, caveats=["Оговорка эксперта: проверил лично."]))

    asyncio.run(_seed_override())

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.recommendation_edited is False
    assert view.caveats_edited is True

    printed_cards = len(view.report.cards) + len(view.report.contest_cards)
    assert printed_cards == 1
    assert recommendation_printable(view, printed_cards) is False
    assert caveats_printable(view, printed_cards) is True

    html = render_print_html(view)
    assert "Первым берём вариант 1 — «Первый»." not in html
    assert "Первый" not in html  # отклонённой карточки в документе нет вовсе
    assert "Оговорка эксперта: проверил лично." in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE in html


def test_правка_только_рекомендации_печатает_независимо_зеркало(db_session_factory, brief):
    """Зеркало предыдущего теста (воспроизведение 2 БЛОК-5, симметрия):
    правит только рекомендацию — проза оговорок модели не печатается, хотя
    рекомендация эксперта на месте."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title="Первый"), _make_card(title="Второй")],
        summary=ReportSummary(
            recommendation="Первым берём вариант 1 — «Первый».",
            caveats=["В карточке 1 риск не подтверждён."],
        ),
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    async def _seed_override() -> None:
        async with db_session_factory() as session:
            session.add(SummaryOverrideRow(request_id=request_id, recommendation="Начать со второго варианта."))

    asyncio.run(_seed_override())

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.recommendation_edited is True
    assert view.caveats_edited is False

    html = render_print_html(view)
    assert "Начать со второго варианта." in html
    assert "В карточке 1 риск не подтверждён." not in html


def test_comparison_table_hidden_при_несовпадении_строк(db_session_factory, brief):
    """РЕШ-212: три карточки, четыре строки сравнения от модели (запасной путь
    или сбойный ответ), карточка 1 отклонена — сопоставить строки с карточками
    нечем, `comparison_table_hidden` поднимается и таблица приходит пустой."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title=f"Карточка {n}") for n in range(1, 4)],
        summary=ReportSummary(
            recommendation="Рекомендация.",
            comparison_rows=[{"option": f"В{n}"} for n in range(1, 5)],
        ),
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.comparison_table_hidden is True
    assert view.report.summary.comparison_rows == []


def test_comparison_table_not_hidden_без_отклонений(db_session_factory, brief):
    """Обратная сторона: несовпадение строк с карточками есть, но ни одна
    карточка не отклонена — `_visible_summary` таблицу не трогает вовсе,
    флаг остаётся `False`."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title=f"Карточка {n}") for n in range(1, 4)],
        summary=ReportSummary(
            recommendation="Рекомендация.",
            comparison_rows=[{"option": f"В{n}"} for n in range(1, 5)],
        ),
    )
    request_id, _card_ids = asyncio.run(_seed_request(db_session_factory, report=report))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.comparison_table_hidden is False
    assert len(view.report.summary.comparison_rows) == 4


def test_comparison_table_not_hidden_когда_строк_нет_с_самого_начала(db_session_factory, brief):
    """Пустая сводка модели (без единой строки, например `fallback_summary`)
    не поднимает `comparison_table_hidden`, даже если карточка отклонена —
    там нечего было прятать с самого начала."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title="Первый"), _make_card(title="Второй")],
        summary=ReportSummary(recommendation="Рекомендация.", comparison_rows=[]),
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.comparison_table_hidden is False
