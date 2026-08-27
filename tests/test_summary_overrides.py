"""Проверки наложения правки эксперта поверх сводки отчёта (РЕШ-206).

Слой чистый: без БД и без сети (инвариант №7). Каждый тест собирает `Report`
из фикстур и проверяет, что наложение не мутирует исходный отчёт и заменяет
поля целиком, а не сливает текст частично.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export.view import ExportView, build_export_view, summary_text_printable
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import SummaryOverride as SummaryOverrideRow
from app.models import Verdict as VerdictRow
from app.schemas import Report, ReportSummary, SolutionCard
from app.schemas import Verdict as VerdictDecision
from app.summary_overrides import SummaryOverrideData, apply_summary_override


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


def test_override_none_возвращает_отчёт_без_изменений(brief):
    """`override is None` — отчёт идентичен исходному побайтово."""
    report = _make_report(brief=brief)

    result = apply_summary_override(report, None)

    assert result.model_dump_json() == report.model_dump_json()


def test_пустая_правка_возвращает_отчёт_без_изменений(brief):
    """Оба поля правки `None` — то же самое, что отсутствие правки вовсе."""
    report = _make_report(brief=brief)

    result = apply_summary_override(report, SummaryOverrideData())

    assert result.model_dump_json() == report.model_dump_json()


def test_рекомендация_заменяется_целиком(brief):
    """Правка рекомендации подменяет текст модели целиком, а не дописывает к нему."""
    report = _make_report(brief=brief)

    result = apply_summary_override(
        report, SummaryOverrideData(recommendation="Начать с варианта 2 (правка эксперта).")
    )

    assert result.summary.recommendation == "Начать с варианта 2 (правка эксперта)."
    # Оговорки не тронуты — правились не они.
    assert result.summary.caveats == ["В карточке 3 нет данных о TRL."]


def test_оговорки_заменяются_целиком(brief):
    """Правка оговорок подменяет список целиком; можно снять все оговорки пустым списком."""
    report = _make_report(brief=brief)

    result = apply_summary_override(report, SummaryOverrideData(caveats=[]))

    assert result.summary.caveats == []
    # Рекомендация не тронута — правилась не она.
    assert result.summary.recommendation == "Первым берём вариант 1."


def test_обе_правки_разом(brief):
    """Рекомендация и оговорки правятся одним вызовом независимо друг от друга."""
    report = _make_report(brief=brief)

    result = apply_summary_override(
        report,
        SummaryOverrideData(recommendation="Начать с варианта 2.", caveats=["Новая оговорка эксперта."]),
    )

    assert result.summary.recommendation == "Начать с варианта 2."
    assert result.summary.caveats == ["Новая оговорка эксперта."]


def test_исходный_report_не_мутируется(brief):
    """Исходный `Report`, переданный в функцию, остаётся неизменным."""
    report = _make_report(brief=brief)

    apply_summary_override(report, SummaryOverrideData(recommendation="Другая рекомендация."))

    assert report.summary.recommendation == "Первым берём вариант 1."
    assert report.summary.caveats == ["В карточке 3 нет данных о TRL."]


def test_is_empty_true_когда_оба_поля_none():
    assert SummaryOverrideData().is_empty() is True
    assert SummaryOverrideData(recommendation=None, caveats=None).is_empty() is True


def test_is_empty_false_если_хотя_бы_одно_поле_задано():
    assert SummaryOverrideData(recommendation="").is_empty() is False
    assert SummaryOverrideData(caveats=[]).is_empty() is False


def _make_view(brief, *, total_cards: int, summary_text_edited: bool) -> ExportView:
    """Минимальный `ExportView` для проверки чистой функции `summary_text_printable`."""
    return ExportView(
        request_id=1,
        report=_make_report(brief=brief),
        card_ids={},
        is_draft=False,
        reviewer_name="",
        validated_at=None,
        total_cards=total_cards,
        summary_text_edited=summary_text_edited,
    )


def test_summary_text_printable_true_когда_текст_правлен_экспертом(brief):
    """Правка эксперта печатается, даже если карточка скрыта."""
    view = _make_view(brief, total_cards=3, summary_text_edited=True)

    assert summary_text_printable(view, printed_cards=2) is True


def test_summary_text_printable_true_когда_ничего_не_скрыто(brief):
    """Текст модели без правки печатается, если ни одна карточка не скрыта."""
    view = _make_view(brief, total_cards=3, summary_text_edited=False)

    assert summary_text_printable(view, printed_cards=3) is True


def test_summary_text_printable_false_когда_карточка_скрыта_и_правки_нет(brief):
    """РЕШ-206: карточка скрыта, правки нет — текст модели печатать нельзя."""
    view = _make_view(brief, total_cards=3, summary_text_edited=False)

    assert summary_text_printable(view, printed_cards=2) is False


# ── Критерий 3: `build_export_view` накладывает правку сводки ──────────────
# База — временный файл SQLite (форма `app.db.session_scope`), как в
# `tests/test_export_view.py`: сеть не задействована (инвариант №7).


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'summary-overrides-test.db'}", future=True)
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
    """Строка `requests` с готовым `report_json` и строками `solution_cards` обеих
    секций. Возвращает id запроса и id строк `solution_cards` по ключу
    (is_contest, position) — по ним заводится вердикт конкретной карточки."""
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


def test_build_export_view_total_cards_обе_секции_до_фильтра(db_session_factory, brief):
    """Критерий 3: `total_cards` — карточки обеих секций до фильтра reject."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title="A"), _make_card(title="B")],
        contest_cards=[_make_card(title="C")],
    )
    request_id, _card_ids = asyncio.run(_seed_request(db_session_factory, report=report))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.total_cards == 3
    assert view.summary_text_edited is False
    assert view.report.summary.recommendation == report.summary.recommendation


def test_build_export_view_total_cards_считает_и_отклонённую_карточку(db_session_factory, brief):
    """Критерий 3 (правка ревью, цикл 2): `total_cards` — счёт ДО фильтра reject,
    а не после. Без действующего вердикта reject «до» и «после» совпадают и не
    ловят мутацию «вычислять total_cards по отфильтрованному отчёту» — здесь
    одна из трёх карточек отклонена, видимых остаётся две, а `total_cards`
    обязан остаться равным трём и быть строго больше числа видимых карточек."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title="A"), _make_card(title="B")],
        contest_cards=[_make_card(title="C")],
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 1)], VerdictDecision.REJECT))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    visible_count = len(view.report.cards) + len(view.report.contest_cards)
    assert visible_count == 2
    assert view.total_cards == 3
    assert view.total_cards > visible_count


def test_build_export_view_отдаёт_правленый_текст_и_summary_text_edited(db_session_factory, brief):
    """Критерий 3: с правкой — `summary_text_edited=True` и правленый текст в `view.report`."""
    report = _make_report(
        brief=brief,
        summary=ReportSummary(recommendation="Исходная рекомендация.", caveats=["Исходная оговорка."]),
    )
    request_id, _card_ids = asyncio.run(_seed_request(db_session_factory, report=report))

    async def _seed_override() -> None:
        async with db_session_factory() as session:
            session.add(
                SummaryOverrideRow(
                    request_id=request_id,
                    recommendation="Правленая рекомендация.",
                    caveats=["Правленая оговорка."],
                )
            )

    asyncio.run(_seed_override())

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.summary_text_edited is True
    assert view.report.summary.recommendation == "Правленая рекомендация."
    assert view.report.summary.caveats == ["Правленая оговорка."]
