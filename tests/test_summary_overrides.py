"""Проверки наложения правки эксперта поверх сводки отчёта (РЕШ-206, РЕШ-211,
РЕШ-212).

Слой чистый: без БД и без сети (инвариант №7), кроме отдельного блока стенда
через `build_export_view` на временном файле SQLite. Каждый тест собирает
`Report` из фикстур и проверяет, что наложение не мутирует исходный отчёт и
заменяет поля целиком, а не сливает текст частично.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export.view import (
    ExportView,
    build_export_view,
    caveats_printable,
    recommendation_printable,
)
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import SummaryOverride as SummaryOverrideRow
from app.models import Verdict as VerdictRow
from app.schemas import Report, ReportSummary, SolutionCard
from app.schemas import Verdict as VerdictDecision
from app.summary_overrides import (
    SummaryOverrideData,
    apply_summary_override,
    is_summary_edited,
    normalize_summary_text,
    resolve_summary_edit,
)


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


def _make_view(
    brief, *, total_cards: int, recommendation_edited: bool = False, caveats_edited: bool = False
) -> ExportView:
    """Минимальный `ExportView` для проверки чистых функций
    `recommendation_printable`/`caveats_printable` (РЕШ-211)."""
    return ExportView(
        request_id=1,
        report=_make_report(brief=brief),
        card_ids={},
        is_draft=False,
        reviewer_name="",
        validated_at=None,
        total_cards=total_cards,
        recommendation_edited=recommendation_edited,
        caveats_edited=caveats_edited,
    )


def test_recommendation_printable_true_когда_рекомендация_правлена_экспертом(brief):
    """Правка эксперта печатается, даже если карточка скрыта."""
    view = _make_view(brief, total_cards=3, recommendation_edited=True)

    assert recommendation_printable(view, printed_cards=2) is True


def test_recommendation_printable_true_когда_ничего_не_скрыто(brief):
    """Текст модели без правки печатается, если ни одна карточка не скрыта."""
    view = _make_view(brief, total_cards=3)

    assert recommendation_printable(view, printed_cards=3) is True


def test_recommendation_printable_false_когда_карточка_скрыта_и_правки_нет(brief):
    """РЕШ-206: карточка скрыта, правки нет — текст модели печатать нельзя."""
    view = _make_view(brief, total_cards=3)

    assert recommendation_printable(view, printed_cards=2) is False


def test_caveats_printable_true_когда_оговорки_правлены_экспертом(brief):
    """Симметрично рекомендации: правка оговорок печатается при скрытой карточке."""
    view = _make_view(brief, total_cards=3, caveats_edited=True)

    assert caveats_printable(view, printed_cards=2) is True


def test_caveats_printable_false_когда_карточка_скрыта_и_правки_нет(brief):
    view = _make_view(brief, total_cards=3)

    assert caveats_printable(view, printed_cards=2) is False


def test_recommendation_и_caveats_printable_решают_независимо_блок5(brief):
    """БЛОК-5 финального аудита, пополевые ворота (РЕШ-211): карточка скрыта,
    правил только оговорки — рекомендация остаётся непечатной, а оговорки
    печатаются. Общий флаг «правлено хоть что-то» дал бы `True` обоим полям —
    ровно тот дефект, который аудит нашёл на живом маршруте: клиенту уезжала
    нетронутая рекомендация модели с советом взять отклонённый вариант."""
    view = _make_view(brief, total_cards=3, caveats_edited=True)

    assert recommendation_printable(view, printed_cards=2) is False
    assert caveats_printable(view, printed_cards=2) is True

    # Зеркало: правил только рекомендацию — оговорки остаются непечатными.
    mirrored = _make_view(brief, total_cards=3, recommendation_edited=True)
    assert recommendation_printable(mirrored, printed_cards=2) is True
    assert caveats_printable(mirrored, printed_cards=2) is False


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
    assert view.recommendation_edited is False
    assert view.caveats_edited is False
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


def test_build_export_view_отдаёт_правленый_текст_и_оба_флага(db_session_factory, brief):
    """Критерий 3: с правкой обоих полей — `recommendation_edited` и
    `caveats_edited` оба `True`, правленый текст в `view.report`."""
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

    assert view.recommendation_edited is True
    assert view.caveats_edited is True
    assert view.report.summary.recommendation == "Правленая рекомендация."
    assert view.report.summary.caveats == ["Правленая оговорка."]


def test_build_export_view_пустая_строка_overrides_не_считается_правкой(db_session_factory, brief):
    """П-8: строка `summary_overrides` существует, но оба поля `None` — это не правка.

    Такая строка появляется в жизни, когда эксперт сохраняет форму без единого
    изменения (`resolve_summary_edit` пишет `None` в оба поля, П-6) — набор из
    1132 тестов прежде не ловил мутацию `summary_text_edited = override is not
    None`, потому что этого сценария нигде не было."""
    report = _make_report(brief=brief)
    request_id, _card_ids = asyncio.run(_seed_request(db_session_factory, report=report))

    async def _seed_empty_override() -> None:
        async with db_session_factory() as session:
            session.add(SummaryOverrideRow(request_id=request_id))

    asyncio.run(_seed_empty_override())

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.recommendation_edited is False
    assert view.caveats_edited is False
    assert view.report.summary.recommendation == report.summary.recommendation


# Стенд-тесты критерия 1 (воспроизведение БЛОК-5 через настоящий
# `build_export_view`) и РЕШ-212 (`comparison_table_hidden`) вынесены в
# `tests/test_summary_field_gate.py` — этот файл уже на пределе 500 строк
# (страж `test_tests_layout.py`), а тем тестам нужен собственный стенд БД.


# ── П-8: `is_summary_edited` — единственная точка расчёта флага правки ─────


def test_is_summary_edited_false_когда_override_none():
    assert is_summary_edited(None) is False


def test_is_summary_edited_false_когда_оба_поля_none():
    """Ловит мутацию `summary_text_edited = override is not None` (П-8):
    объект правки существует (`override is not None`), но обе правки пусты —
    флаг обязан остаться `False`."""
    assert is_summary_edited(SummaryOverrideData()) is False
    assert is_summary_edited(SummaryOverrideData(recommendation=None, caveats=None)) is False


def test_is_summary_edited_true_если_хотя_бы_одно_поле_задано():
    assert is_summary_edited(SummaryOverrideData(recommendation="")) is True
    assert is_summary_edited(SummaryOverrideData(caveats=[])) is True
    assert is_summary_edited(SummaryOverrideData(recommendation="Другая рекомендация.")) is True


# ── Критерий 1 (П-6): дословное совпадение с исходником — не правка ────────


def test_normalize_summary_text_обрезает_пробелы_и_пустые_строки_по_краям():
    assert normalize_summary_text("  Текст.  \n\n") == "Текст."
    assert normalize_summary_text("\nТекст.") == "Текст."
    # Внутренние переносы строк нормализация не трогает.
    assert normalize_summary_text("Первая строка.\nВторая строка.") == "Первая строка.\nВторая строка."


def test_resolve_summary_edit_дословное_совпадение_не_считается_правкой():
    """Критерий 1: пробелы и пустые строки по краям формы не превращают
    сохранение исходного текста в правку."""
    resolved = resolve_summary_edit(
        original_recommendation="Первым берём вариант 1.",
        original_caveats=["В карточке 3 нет данных о TRL."],
        submitted_recommendation="  Первым берём вариант 1.  \n",
        submitted_caveats=["В карточке 3 нет данных о TRL."],
    )

    assert resolved.recommendation is None
    assert resolved.caveats is None
    assert is_summary_edited(resolved) is False


def test_resolve_summary_edit_правит_только_изменённое_поле():
    """«Правкой считается изменённый текст»: если эксперт поменял только
    рекомендацию, оговорки остаются `None` («не правил») и наоборот."""
    only_recommendation = resolve_summary_edit(
        original_recommendation="Первым берём вариант 1.",
        original_caveats=["Оговорка модели."],
        submitted_recommendation="Начать с варианта 2.",
        submitted_caveats=["Оговорка модели."],
    )
    assert only_recommendation.recommendation == "Начать с варианта 2."
    assert only_recommendation.caveats is None

    only_caveats = resolve_summary_edit(
        original_recommendation="Первым берём вариант 1.",
        original_caveats=["Оговорка модели."],
        submitted_recommendation="Первым берём вариант 1.",
        submitted_caveats=["Оговорка модели.", "Новая оговорка эксперта."],
    )
    assert only_caveats.recommendation is None
    assert only_caveats.caveats == ["Оговорка модели.", "Новая оговорка эксперта."]


def test_resolve_summary_edit_реальная_правка_сохраняется_целиком():
    resolved = resolve_summary_edit(
        original_recommendation="Первым берём вариант 1.",
        original_caveats=[],
        submitted_recommendation="Начать с варианта 2 (правка эксперта).",
        submitted_caveats=["Новая оговорка."],
    )

    assert resolved.recommendation == "Начать с варианта 2 (правка эксперта)."
    assert resolved.caveats == ["Новая оговорка."]
    assert is_summary_edited(resolved) is True
