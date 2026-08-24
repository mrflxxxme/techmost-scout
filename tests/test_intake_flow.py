"""Сценарий интейка (`app.pipeline.intake_flow`) без CLI (И-4, РЕШ-184).

Модуль вынесен из `cli_run.py` так, чтобы веб (Э3, И-9) звал ровно тот же
код, что и командная строка. Здесь проверяется сам модуль напрямую — без
`typer`, без `console`, только через его функции: отказ `run_intake`, отказ
`refine_brief`, оба стоп-крана (`CostLimitExceededError` пробрасывается наружу
и оставляет строку `failed` в записи), пустые ответы, оговорки в
возвращаемом значении. `tests/test_cli_run.py` не тронут (доказательство —
отдельная задача, `git diff --stat`), эти тесты — независимая граница.

Сети нет (инвариант №7): провайдер модели — заглушка `FakeProvider`
(`tests/conftest.py`), база — временный SQLite в `tmp_path`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.llm import CostLimitExceededError, LLMRouter
from app.models import Base
from app.models import Request as RequestRow
from app.pipeline.intake_flow import (
    INTAKE_FALLBACK_CAVEAT,
    REFINE_FALLBACK_CAVEAT,
    STAGE_INTAKE,
    STAGE_INTAKE_REFINE,
    refine_brief_stage,
    resolve_constraints_stage,
    run_intake_stage,
)
from app.pipeline.persistence import StageRecord
from app.schemas import Brief, IntakeQuestion, IntakeResult


@pytest.fixture
async def db_session_factory(tmp_path):
    """Фабрика сессий на временном SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'intake-flow.db'}", future=True)
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


def _router(settings, fake_provider_factory) -> LLMRouter:
    return LLMRouter(settings=settings, providers={"deepseek": fake_provider_factory("deepseek")})


def _draft_brief(*, constraints: list[str] | None = None) -> Brief:
    return Brief(
        industry="нефтепереработка",
        task_summary="Катализатор гидроочистки теряет активность раньше срока.",
        constraints=constraints or [],
    )


async def _requests_rows(session_factory) -> list[RequestRow]:
    async with session_factory() as session:
        return list((await session.execute(select(RequestRow))).scalars().all())


# ── Отказ run_intake ──────────────────────────────────────────────────────


async def test_run_intake_failure_returns_fallback_brief_with_caveat(
    monkeypatch, settings, fake_provider_factory, db_session_factory
):
    """Отказ `run_intake` не бросает исключение: возвращается готовый запасной
    бриф, оговорка `INTAKE_FALLBACK_CAVEAT` и запись `intake` с ошибкой в
    журнале — но без строки в базе (прогон продолжается, а не падает)."""
    router = _router(settings, fake_provider_factory)

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)

    stage_log = []
    outcome = await run_intake_stage(
        router,
        "текст задачи про катализатор",
        industry_hint="нефтепереработка",
        session_factory=db_session_factory,
        stage_log=stage_log,
    )

    assert outcome.intake is None
    assert outcome.brief is not None
    assert outcome.brief.task_summary == "текст задачи про катализатор"
    assert outcome.caveats == [INTAKE_FALLBACK_CAVEAT]
    assert outcome.warning and "интейк не удался" in outcome.warning

    assert len(stage_log) == 1
    assert stage_log[0].stage == STAGE_INTAKE
    assert stage_log[0].error

    # Устойчивость интейка (Т-2): отказ не роняет прогон, `requests` пуст —
    # прогон продолжается дальше, следа падения ещё нет.
    assert await _requests_rows(db_session_factory) == []


# ── Отказ refine_brief ───────────────────────────────────────────────────


async def test_refine_brief_failure_keeps_owner_answers_in_own_field(
    monkeypatch, settings, fake_provider_factory, db_session_factory
):
    """Отказ `refine_brief` не роняет прогон: ответы владельца ложатся в
    `Brief.owner_answers` черновика интейка (не в `constraints`), а клиент
    получает оговорку `REFINE_FALLBACK_CAVEAT`."""
    router = _router(settings, fake_provider_factory)
    draft = _draft_brief(constraints=["работа при 340 °C"])
    intake = IntakeResult(
        questions=[IntakeQuestion(question="Какая температура процесса?", why="нужна для отбора решений")],
        draft_brief=draft,
    )

    async def broken_refine_brief(router, task, answers, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.pipeline.stages.refine_brief", broken_refine_brief)

    stage_log = []
    outcome = await refine_brief_stage(
        router,
        "текст задачи",
        intake,
        {"Какая температура процесса?": "380 °C"},
        industry_hint="нефтепереработка",
        session_factory=db_session_factory,
        stage_log=stage_log,
    )

    assert outcome.brief.constraints == ["работа при 340 °C"], (
        "ответы владельца не имеют права попадать в ограничения задачи"
    )
    assert [(item.question, item.answer) for item in outcome.brief.owner_answers] == [
        ("Какая температура процесса?", "380 °C")
    ]
    assert outcome.caveats == [REFINE_FALLBACK_CAVEAT]
    assert outcome.warning and "уточнение брифа не удалось" in outcome.warning

    assert len(stage_log) == 1
    assert stage_log[0].stage == STAGE_INTAKE_REFINE
    assert stage_log[0].error


# ── Пустые ответы ─────────────────────────────────────────────────────────


async def test_refine_brief_stage_with_empty_answers_still_succeeds(
    monkeypatch, settings, fake_provider_factory, db_session_factory
):
    """Модуль сам не решает, звать ли уточнение при пустом словаре ответов
    (это решение вызывающей стороны — постановка модуля), но обязан
    корректно отработать и на пустом словаре, не потеряв оговорок и не
    уронив прогон — на случай, если вызывающая сторона всё же его позовёт."""
    router = _router(settings, fake_provider_factory)
    draft = _draft_brief()
    intake = IntakeResult(questions=[], draft_brief=draft)

    async def fake_refine_brief(router, task, answers, *, industry_hint="не указана"):
        assert answers == {}
        return draft.model_copy(update={"task_summary": "Уточнённая постановка без ответов."})

    monkeypatch.setattr("app.pipeline.stages.refine_brief", fake_refine_brief)

    stage_log = []
    outcome = await refine_brief_stage(
        router,
        "текст задачи",
        intake,
        {},
        industry_hint="нефтепереработка",
        session_factory=db_session_factory,
        stage_log=stage_log,
    )

    assert outcome.brief.task_summary == "Уточнённая постановка без ответов."
    assert outcome.caveats == []
    assert outcome.warning is None
    assert len(stage_log) == 1 and stage_log[0].error is None


# ── Оговорки в возвращаемом значении: здоровый путь их не оставляет ──────


async def test_successful_run_intake_stage_has_no_caveats_and_no_warning(
    monkeypatch, settings, fake_provider_factory, db_session_factory
):
    """Обратная сторона отказа: удачный разбор не оставляет клиенту ни
    оговорки, ни предупреждения — иначе клиент читал бы предупреждение о
    качестве выдачи на каждом здоровом прогоне."""
    router = _router(settings, fake_provider_factory)
    draft = _draft_brief(constraints=["работа при 340 °C"])

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(questions=[], draft_brief=draft)

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    stage_log = []
    outcome = await run_intake_stage(
        router,
        "текст задачи",
        industry_hint="нефтепереработка",
        session_factory=db_session_factory,
        stage_log=stage_log,
    )

    assert outcome.intake is not None and outcome.brief is None
    assert outcome.caveats == []
    assert outcome.warning is None
    assert len(stage_log) == 1
    assert stage_log[0].stage == STAGE_INTAKE
    assert stage_log[0].error is None


# ── Оба стоп-крана ────────────────────────────────────────────────────────


async def test_run_intake_cost_limit_propagates_and_persists_failed(
    monkeypatch, settings, fake_provider_factory, db_session_factory
):
    """РЕШ-45: стоп-кран на `run_intake` — команда «стоп тратить», не отказ
    модели. Исключение пробрасывается наружу, а `requests` получает строку
    `failed` — деньги на неудавшийся вызов уже потрачены, и трата не должна
    пропасть без следа."""
    router = _router(settings, fake_provider_factory)

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        raise CostLimitExceededError("запрос израсходовал 10.00 ₽ при лимите 10.00 ₽")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)

    stage_log = []
    with pytest.raises(CostLimitExceededError):
        await run_intake_stage(
            router,
            "текст задачи",
            industry_hint="нефтепереработка",
            session_factory=db_session_factory,
            stage_log=stage_log,
        )

    rows = await _requests_rows(db_session_factory)
    assert len(rows) == 1, "оборванный стоп-краном прогон обязан оставить строку в requests"
    assert rows[0].status == "failed"
    assert rows[0].error

    assert len(stage_log) == 1
    assert stage_log[0].stage == STAGE_INTAKE
    assert stage_log[0].error


async def test_resolve_constraints_cost_limit_propagates_and_persists_failed(
    monkeypatch, settings, fake_provider_factory, db_session_factory
):
    """Второй стоп-кран сценария — на дозаполнении ограничений (РЕШ-115).
    `retry_constraints_if_missing` уже дописывает свою строку в `stage_log`
    перед броском (`record=None` в `_persist_intake_stop`), поэтому здесь
    дубля записи быть не должно, а строка в базе — быть обязана."""
    router = _router(settings, fake_provider_factory)
    brief = _draft_brief()

    async def broken_retry(router, brief, raw_text, *, industry_hint, stage_log):
        now = datetime.now(UTC)
        stage_log.append(
            StageRecord(
                stage="intake_constraints",
                started_at=now,
                finished_at=now,
                error="запрос израсходовал 10.00 ₽ при лимите 10.00 ₽",
            )
        )
        raise CostLimitExceededError("запрос израсходовал 10.00 ₽ при лимите 10.00 ₽")

    monkeypatch.setattr("app.pipeline.constraints_guard.retry_constraints_if_missing", broken_retry)

    stage_log = []
    with pytest.raises(CostLimitExceededError):
        await resolve_constraints_stage(
            router,
            brief,
            "Требуемая точность 95-98%, TRL 6-9",
            industry_hint="фарма",
            session_factory=db_session_factory,
            stage_log=stage_log,
        )

    rows = await _requests_rows(db_session_factory)
    assert len(rows) == 1
    assert rows[0].status == "failed"

    # record=None: повторного append здесь быть не должно — ровно одна
    # запись, та, что дописала сама заглушка retry.
    assert len(stage_log) == 1
    assert stage_log[0].stage == "intake_constraints"
    assert stage_log[0].error
