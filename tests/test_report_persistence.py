"""Круговой перегон отчёта через БД (РЕШ-146/156): report_json и briefs.

Отчёт живёт только markdown-файлом (Т-7 и раньше): `requests.report_json`
нет, `briefs` мертва (РЕШ-156) — ничего в неё не пишет ни разу. Здесь
проверяется, что задача Д-1 это чинит: `persist_run` кладёт `Report` целиком
в `report_json` с уже исправленным `request_id` (а не нулём, который стоит в
объекте на момент записи, см. докстроку `persistence.persist_run`);
`load_report` читает его назад типизированным `Report`, а не сырым словарём
(мутация б задачи); строка `briefs` пишется на КАЖДОМ прогоне, где бриф уже
собран, — успешном и упавшем на любой стадии после интейка, — а ответы
владельца доезжают до базы без потерь.

Отдельно — круговой перегон схемы `Report → JSON → Report` независимо от БД:
если сериализация теряет поле, платный прогон узнал бы об этом только на
экране Э3-5, когда чинить это будет дороже всего.

База — временный файл SQLite (`tmp_path`), сеть не задействована — тот же
приём, что и в tests/test_persistence.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.models import Brief as BriefRow
from app.models import Request as RequestRow
from app.pipeline.persistence import ReportNotFoundError, load_report, persist_run
from app.schemas import (
    Brief,
    Confidence,
    Evidence,
    GovSupportMatch,
    IPStatus,
    MaturityStage,
    OwnerAnswer,
    Report,
    ReportSummary,
    RequirementCheck,
    SolutionCard,
)


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — та же форма, что в test_persistence.py."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'report-test.db'}", future=True)
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


def _full_card(**overrides) -> SolutionCard:
    """Карточка со всеми полями, заполненными непустыми/нестандартными
    значениями: сравнение "по умолчанию" не поймало бы поле, забытое
    сериализацией, потому что умолчание и есть пустое значение."""
    fields = {
        "title": "Катализатор с присадками для сероустойчивости",
        "essence": "Присадка продлевает межрегенерационный цикл на сырье с серой.",
        "maturity_stage": MaturityStage.PILOT,
        "trl_estimate": 6,
        "ip_status": IPStatus.TEAM,
        "developer_org": "Институт катализа",
        "developer_team": "Лаборатория гидроочистки",
        "contact_leads": ["ivanov@example.ru"],
        "evidence": [Evidence(claim="Испытан на действующей установке", url="https://doi.org/10.1/test-report")],
        "applicability": "Закрывает задачу продления цикла.",
        "risks": ["Нужна опытная партия присадки"],
        "gov_support": [
            GovSupportMatch(
                instrument="ФСИ Старт", why="Подходит по стадии", limit="до 4 млн ₽", url="https://fasie.ru"
            )
        ],
        "next_step": "Запросить протокол испытаний.",
        "confidence": Confidence.MEDIUM,
        "confidence_note": "Один источник",
        "candidate_urls": ["https://doi.org/10.1/test-report"],
        "requirement_checks": [
            RequirementCheck(
                requirement="Стойкость к сере не ниже 2%",
                status="confirmed",
                evidence_url="https://doi.org/10.1/test-report",
                note="прямо указано в аннотации",
            ),
        ],
        "unsourced_claims": ["Снижение затрат на 30% источником не подтверждено"],
        "pdf_urls": ["https://arxiv.org/pdf/2501.00001"],
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _full_report(request_id: int = 7) -> Report:
    """Отчёт со всеми полями непустыми — образец для кругового перегона (критерий Д-1)."""
    return Report(
        request_id=request_id,
        brief=Brief(
            industry="нефтепереработка",
            task_summary="Катализатор гидроочистки теряет активность за 8 месяцев вместо 24.",
            constraints=["работа при 340 °C", "сырьё с повышенным содержанием серы"],
            owner_answers=[OwnerAnswer(question="Бюджет проекта?", answer="требование не специализировано")],
            target_maturity=MaturityStage.PILOT,
            geography="РФ",
            budget_hint="до 40 млн ₽",
            timeline_hint="6 месяцев",
            keywords_ru=["катализатор гидроочистки"],
            keywords_en=["hydrotreating catalyst"],
        ),
        cards=[_full_card()],
        contest_cards=[_full_card(title="Хакатонная команда «Сероочистка»")],
        summary=ReportSummary(
            comparison_rows=[{"вариант": "1", "срок": "6 мес"}],
            recommendation="Рассмотреть вариант №1.",
            caveats=["Автоматический поиск не дал кандидатов ещё по двум формулировкам"],
        ),
        sources_unavailable=["semantic_scholar"],
        constraints_unresolved=True,
        generated_at=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
        cost_rub=4.56,
    )


# ── Критерий Д-1: сериализация Report → JSON → Report обратима ──────────────
def test_report_round_trips_through_json_without_field_loss():
    """`model_dump_json` → `model_validate_json` не теряет ни одного поля —
    включая вложенные списки карточек, брифа с owner_answers и сводки."""
    report = _full_report()

    restored = Report.model_validate_json(report.model_dump_json())

    assert isinstance(restored, Report)
    assert restored.model_dump(mode="json") == report.model_dump(mode="json")


# ── persist_run пишет report_json, load_report читает его типизированным ────
async def test_persist_run_writes_report_json_and_load_report_reads_it_back(db_session_factory):
    """`persist_run(report=...)` кладёт JSON в `requests.report_json` с уже
    исправленным `request_id`; `load_report` отдаёт типизированный `Report`,
    а не сырой словарь (мутация б) — совпадающий по содержимому (мутация а)."""
    report = _full_report(request_id=0)  # 0 — как до записи, `_write` исправит на настоящий

    persisted_id = await persist_run(
        db_session_factory,
        raw_text="текст задачи с отчётом целиком",
        status="drafted",
        cost_rub=report.cost_rub,
        cards=report.cards,
        contest_cards=report.contest_cards,
        stages=[],
        brief=report.brief,
        report=report,
    )
    assert persisted_id is not None

    async with db_session_factory() as session:
        row = (await session.execute(select(RequestRow))).scalars().one()
    assert row.report_json, "мутация (а): report_json не пишется — пустое поле роняет этот тест"

    loaded = await load_report(db_session_factory, persisted_id)
    assert isinstance(loaded, Report), "мутация (б): сырой dict не прошёл бы isinstance"

    expected = report.model_copy(update={"request_id": persisted_id})
    assert loaded.model_dump(mode="json") == expected.model_dump(mode="json")


async def test_load_report_raises_on_unknown_id(db_session_factory):
    """Несуществующий номер — понятный отказ с номером в тексте, а не `None`
    (по образцу `test_load_search_plan_raises_on_unknown_id`)."""
    with pytest.raises(ReportNotFoundError) as info:
        await load_report(db_session_factory, 4242)
    assert "4242" in str(info.value)


async def test_load_report_raises_when_report_json_is_empty(db_session_factory):
    """Строка `requests` есть (отказавший прогон), но `report_json` пуст —
    отчёта для клиента ещё не существует, читать нечего."""
    persisted_id = await persist_run(
        db_session_factory,
        raw_text="текст задачи, упавшей до сборки отчёта",
        status="failed",
        cost_rub=0.0,
        cards=[],
        contest_cards=[],
        stages=[],
        error="стадия карточек отказала",
    )
    assert persisted_id is not None

    with pytest.raises(ReportNotFoundError):
        await load_report(db_session_factory, persisted_id)


async def test_load_report_raises_without_factory():
    """Фабрики нет — читать нечем, отказ явный, а не тихий `None`."""
    with pytest.raises(ReportNotFoundError):
        await load_report(None, 1)


# ── briefs оживает: строка пишется на прогоне, ответы пустые в авто-режиме ──
async def test_persist_run_writes_brief_row_with_empty_owner_answers_in_auto_mode(db_session_factory):
    """Таблица `briefs` была мертва (РЕШ-156): строка появляется на прогоне,
    даже когда владелец не отвечал ни на один уточняющий вопрос (авто-режим)."""
    brief = Brief(
        industry="нефтепереработка",
        task_summary="Катализатор теряет активность раньше срока.",
    )

    persisted_id = await persist_run(
        db_session_factory,
        raw_text="текст задачи в авто-режиме",
        status="drafted",
        cost_rub=0.0,
        cards=[],
        contest_cards=[],
        stages=[],
        brief=brief,
    )

    async with db_session_factory() as session:
        rows = (await session.execute(select(BriefRow))).scalars().all()

    assert len(rows) == 1  # критерий Д-1: briefs > 0 строк после прогона
    assert rows[0].request_id == persisted_id
    assert rows[0].industry == "нефтепереработка"
    assert rows[0].owner_answers == []  # авто-режим — вопросов не задавали, но строка есть


async def test_persist_run_writes_brief_owner_answers_when_present(db_session_factory):
    """Ответы владельца на уточняющие вопросы доезжают до БД без потерь."""
    brief = Brief(
        industry="фарма",
        task_summary="Диагностика супоросности свиноматок по УЗИ.",
        owner_answers=[
            OwnerAnswer(question="Какой бюджет проекта?", answer="требование не специализировано"),
            OwnerAnswer(question="Есть ли сроки внедрения?", answer="полгода"),
        ],
    )

    await persist_run(
        db_session_factory,
        raw_text="текст задачи с ответами владельца",
        status="drafted",
        cost_rub=0.0,
        cards=[],
        contest_cards=[],
        stages=[],
        brief=brief,
    )

    async with db_session_factory() as session:
        row = (await session.execute(select(BriefRow))).scalars().one()

    assert row.owner_answers == [
        {"question": "Какой бюджет проекта?", "answer": "требование не специализировано"},
        {"question": "Есть ли сроки внедрения?", "answer": "полгода"},
    ]


async def test_persist_run_writes_brief_row_on_failed_run_too(db_session_factory):
    """Бриф существует уже к моменту отказа любой стадии после интейка — строка
    `briefs` пишется и при статусе `failed`, а не только на успешном прогоне."""
    brief = Brief(industry="нефтегаз", task_summary="Задача, которая упадёт на скоринге.")

    await persist_run(
        db_session_factory,
        raw_text="текст задачи, упавшей после интейка",
        status="failed",
        cost_rub=0.02,
        cards=[],
        contest_cards=[],
        stages=[],
        error="LLMError: стадия скоринга отказала",
        brief=brief,
    )

    async with db_session_factory() as session:
        rows = (await session.execute(select(BriefRow))).scalars().all()

    assert len(rows) == 1
    assert rows[0].industry == "нефтегаз"
