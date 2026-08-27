"""Проверки единственного сборщика данных отчёта и выгрузок (Э4-И2, РЕШ-196 п. 8).

База — временный файл SQLite на `tmp_path` (форма `app.db.session_scope`), как
в `tests/test_web_report.py` и `tests/test_persistence.py`: сеть не
задействована (инвариант №7).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export.view import build_export_view
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import Verdict as VerdictRow
from app.schemas import Evidence, Report, ReportSummary, SolutionCard
from app.schemas import Verdict as VerdictDecision
from app.verdicts import visible_report


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — та же форма, что `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'export-view-test.db'}", future=True)
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


def _make_card(**overrides) -> SolutionCard:
    fields = {
        "title": "Регенерация катализатора добавками молибдена",
        "essence": "Продление срока службы катализатора за счёт присадки.",
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "next_step": "Запросить образец у разработчика.",
        "evidence": [Evidence(claim="Испытан на действующей установке", url="https://example.org/a")],
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


async def _seed(
    session_factory,
    *,
    report: Report,
    status: RequestStatus = RequestStatus.DRAFTED,
    card_overrides: dict[int, dict] | None = None,
    with_card_rows: bool = True,
) -> tuple[int, list[int]]:
    """Строка `requests` с готовым `report_json` и строками `solution_cards`
    того же состава по порядку; `card_overrides` — правки по индексу карточки
    в `report.cards` (секция `is_contest=False`). Возвращает id запроса и
    список id строк `solution_cards` в порядке `report.cards`."""
    card_overrides = card_overrides or {}
    async with session_factory() as session:
        row = RequestRow(
            raw_text="катализатор теряет активность раньше срока",
            status=status,
            cost_rub=report.cost_rub,
            report_json=report.model_dump_json(),
        )
        session.add(row)
        await session.flush()
        request_id = int(row.id)
        card_ids: list[int] = []
        if with_card_rows:
            for position, card in enumerate(report.cards):
                card_row = SolutionCardRow(
                    request_id=request_id,
                    position=position,
                    is_contest=False,
                    title=card.title,
                    essence=card.essence,
                    maturity_stage=card.maturity_stage.value,
                    applicability=card.applicability,
                    next_step=card.next_step,
                    evidence=[item.model_dump(mode="json") for item in card.evidence],
                    confidence=card.confidence.value,
                    editor_overrides=card_overrides.get(position, {}),
                )
                session.add(card_row)
                await session.flush()
                card_ids.append(int(card_row.id))
    return request_id, card_ids


async def _add_verdict(
    session_factory, card_id: int, decision: VerdictDecision, *, reviewer_name: str = "Иван Иванов"
) -> None:
    async with session_factory() as session:
        session.add(VerdictRow(card_id=card_id, reviewer_name=reviewer_name, decision=decision.value))


async def test_reject_карточка_не_попадает_в_view(db_session_factory, brief):
    """Карточка с действующим вердиктом reject не попадает в `view.report`."""
    kept = _make_card(title="Оставленная карточка")
    rejected = _make_card(title="Отклонённая карточка")
    report = _make_report([kept, rejected], brief=brief)
    request_id, card_ids = await _seed(db_session_factory, report=report)
    await _add_verdict(db_session_factory, card_ids[1], VerdictDecision.REJECT)

    async with db_session_factory() as session:
        view = await build_export_view(session, request_id)

    assert [card.title for card in view.report.cards] == ["Оставленная карточка"]


async def test_rework_попадает_в_отчёт_и_в_rework_keys(db_session_factory, brief):
    """`rework` карточку не убирает, но её ключ есть в `rework_keys`."""
    card = _make_card(title="Требует доработки")
    report = _make_report([card], brief=brief)
    request_id, card_ids = await _seed(db_session_factory, report=report)
    await _add_verdict(db_session_factory, card_ids[0], VerdictDecision.REWORK)

    async with db_session_factory() as session:
        view = await build_export_view(session, request_id)

    assert [c.title for c in view.report.cards] == ["Требует доработки"]
    assert (False, 0) in view.rework_keys


async def test_правки_editor_overrides_наложены_до_фильтра(db_session_factory, brief):
    """Правленый заголовок виден: наложение идёт раньше фильтра reject."""
    card = _make_card(title="Исходный заголовок")
    report = _make_report([card], brief=brief)
    request_id, card_ids = await _seed(
        db_session_factory, report=report, card_overrides={0: {"title": "Заголовок оператора"}}
    )
    await _add_verdict(db_session_factory, card_ids[0], VerdictDecision.APPROVE)

    async with db_session_factory() as session:
        view = await build_export_view(session, request_id)

    assert view.report.cards[0].title == "Заголовок оператора"


async def test_отсутствие_solution_cards_не_роняет_сборку(db_session_factory, brief):
    """Строк `solution_cards` нет вовсе — правок нет, сборка не падает."""
    card = _make_card()
    report = _make_report([card], brief=brief)
    request_id, _ = await _seed(db_session_factory, report=report, with_card_rows=False)

    async with db_session_factory() as session:
        view = await build_export_view(session, request_id)

    assert [c.title for c in view.report.cards] == [card.title]
    assert view.card_ids == {}
    assert view.rework_keys == set()
    assert view.reviewer_name == ""
    assert view.validated_at is None


async def test_is_draft_снимается_только_при_validated_или_exported(db_session_factory, brief):
    """`is_draft` соответствует статусу запроса (инвариант №2)."""
    report = _make_report([_make_card()], brief=brief)
    request_id, _ = await _seed(db_session_factory, report=report, status=RequestStatus.VALIDATED)

    async with db_session_factory() as session:
        view = await build_export_view(session, request_id)

    assert view.is_draft is False


async def test_reviewer_name_и_validated_at_из_последнего_вердикта(db_session_factory, brief):
    """Подпись строится из действующего вердикта, а не из настройки окружения."""
    card = _make_card()
    report = _make_report([card], brief=brief)
    request_id, card_ids = await _seed(db_session_factory, report=report, status=RequestStatus.VALIDATED)
    await _add_verdict(db_session_factory, card_ids[0], VerdictDecision.APPROVE, reviewer_name="Пётр Петров")

    async with db_session_factory() as session:
        view = await build_export_view(session, request_id)

    assert view.reviewer_name == "Пётр Петров"
    assert view.validated_at is not None


async def test_reject_в_середине_списка_переиндексирует_card_ids_и_rework_keys(db_session_factory, brief):
    """Reject в середине списка из 4 карточек (риск из PLAN.md «Риски»): id и
    пометка rework обязаны сдвинуться на послефильтровые позиции, а не
    остаться на дофильтровых — иначе правка или бейдж «уедут» не на ту
    карточку. Вырожденные случаи (0/1 карточка после reject, reject
    последней) этого не ловят — нужен reject строго в середине 3+ карточек."""
    a = _make_card(title="A")
    b = _make_card(title="B")
    c = _make_card(title="C")
    d = _make_card(title="D")
    report = _make_report([a, b, c, d], brief=brief)
    request_id, card_ids = await _seed(db_session_factory, report=report)
    a_id, b_id, c_id, d_id = card_ids
    await _add_verdict(db_session_factory, b_id, VerdictDecision.REJECT)
    await _add_verdict(db_session_factory, d_id, VerdictDecision.REWORK)

    async with db_session_factory() as session:
        view = await build_export_view(session, request_id)

    assert [card.title for card in view.report.cards] == ["A", "C", "D"]
    # Позиция 1 после фильтра обязана указывать на C (её дофильтровая позиция
    # была 2), а не на B (отклонена) и не остаться дофильтровой парой (False,2).
    assert view.card_ids == {(False, 0): a_id, (False, 1): c_id, (False, 2): d_id}
    # D была на дофильтровой позиции 3 — послефильтровая пометка rework обязана
    # сдвинуться на (False, 2), а не остаться (False, 3).
    assert view.rework_keys == {(False, 2)}


async def test_правка_карточки_после_reject_остаётся_на_своей_карточке(db_session_factory, brief):
    """`editor_overrides`, записанные в БД под дофильтровой позицией карточки C
    (позиция 2, ПОСЛЕ отклонённой B), обязаны остаться на C и после фильтра —
    порядок «правки до фильтра» не вырожденный случай с одной карточкой, а
    минимум с тремя, одна из которых reject. Если фильтр сработает раньше
    наложения правок, правка «уедет» на карточку D, которая после фильтра
    окажется на той же дофильтровой позиции 2, что и правка."""
    a = _make_card(title="A")
    b = _make_card(title="B")
    c = _make_card(title="C — исходный заголовок")
    d = _make_card(title="D")
    report = _make_report([a, b, c, d], brief=brief)
    request_id, card_ids = await _seed(
        db_session_factory, report=report, card_overrides={2: {"title": "C — правка оператора"}}
    )
    _a_id, b_id, c_id, _d_id = card_ids
    await _add_verdict(db_session_factory, b_id, VerdictDecision.REJECT)

    async with db_session_factory() as session:
        view = await build_export_view(session, request_id)

    assert [card.title for card in view.report.cards] == ["A", "C — правка оператора", "D"]
    assert view.card_ids[(False, 1)] == c_id


def test_visible_report_не_мутирует_входной_report(brief):
    """Совет ревью Э4-И1: `visible_report` не должна мутировать переданный
    `Report` — по образцу `test_report_overrides.py::
    test_original_report_is_not_mutated`. Проверяется напрямую на `verdicts.
    visible_report`, поскольку именно её вызывает `build_export_view`."""
    kept = _make_card(title="Оставленная карточка")
    rejected = _make_card(title="Отклонённая карточка")
    report = _make_report([kept, rejected], brief=brief)
    effective = {2: VerdictRow(id=1, card_id=2, reviewer_name="Иван Иванов", decision=VerdictDecision.REJECT.value)}

    visible_report(report, {(False, 0): 1, (False, 1): 2}, effective)

    assert [card.title for card in report.cards] == ["Оставленная карточка", "Отклонённая карточка"]
