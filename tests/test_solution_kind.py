"""Вид решения в брифе (Э6-1): выбор человека сильнее модели, хранение, форма.

База — временный файл SQLite, модель — заглушка роутера, сети нет.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import add_missing_columns
from app.models import Base
from app.models import Request as RequestRow
from app.pipeline.intake import refine_brief, resolve_solution_kind, run_intake
from app.pipeline.intake_flow import run_intake_stage
from app.schemas import Brief, IntakeResult
from app.web import brief_store

KINDS = ["research", "product", "digital", "business_model"]


class _StubRouter:
    """Заглушка `LLMRouter`: отдаёт заранее заданный ответ и запоминает промпт."""

    cost = SimpleNamespace(by_provider={}, spent_rub=0.0)

    def __init__(self, draft_kind: Any) -> None:
        self.draft_kind = draft_kind
        self.prompts: list[str] = []

    async def complete_json(self, stage: str, messages: list, schema: type, **_: Any) -> Any:
        self.prompts.append(messages[0].content)
        draft: dict[str, Any] = {"industry": "отрасль", "task_summary": "суть"}
        if self.draft_kind is not None:
            draft["solution_kind"] = self.draft_kind
        return schema.model_validate_json(json.dumps({"questions": [], "draft_brief": draft}))


# ── resolve_solution_kind ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("selected", "model_value", "expected"),
    [
        ("research", "digital", "research"),  # выбор человека сильнее модели
        ("business_model", "product", "business_model"),
        ("unknown", "digital", "digital"),  # unknown: берём значение модели
        ("unknown", "research", "product"),  # research при unknown не принимается
        ("unknown", "что-то своё", "product"),
    ],
)
def test_resolve_solution_kind(selected: str, model_value: str, expected: str):
    assert resolve_solution_kind(selected, model_value) == expected


# ── разбор ответа модели ──────────────────────────────────────────────────────
def test_ответ_модели_со_значением_вне_перечня_не_валит_разбор():
    payload = {"questions": [], "draft_brief": {"industry": "о", "task_summary": "с", "solution_kind": "unknown"}}

    assert IntakeResult.model_validate(payload).draft_brief.solution_kind == "research"


# ── стадии интейка ────────────────────────────────────────────────────────────
async def test_run_intake_выбор_человека_перекрывает_ответ_модели():
    router = _StubRouter("digital")

    result = await run_intake(router, "текст", solution_kind="product")

    assert result.draft_brief.solution_kind == "product"


async def test_run_intake_unknown_берёт_вид_модели_и_метка_заполнена():
    router = _StubRouter("business_model")

    result = await run_intake(router, "текст", solution_kind="unknown")

    assert result.draft_brief.solution_kind == "business_model"
    assert "{{SOLUTION_KIND}}" not in router.prompts[0]


async def test_run_intake_unknown_и_research_модели_даёт_product():
    result = await run_intake(_StubRouter("research"), "текст", solution_kind="unknown")

    assert result.draft_brief.solution_kind == "product"


async def test_run_intake_по_умолчанию_research():
    result = await run_intake(_StubRouter(None), "текст")

    assert result.draft_brief.solution_kind == "research"


async def test_refine_brief_несёт_вид_решения():
    brief = await refine_brief(_StubRouter("digital"), "текст", {"в?": "о"}, solution_kind="product")

    assert brief.solution_kind == "product"


async def test_run_intake_stage_передаёт_вид_а_запасной_бриф_его_сохраняет():
    ok = await run_intake_stage(
        _StubRouter("digital"),
        "текст",
        industry_hint="н",
        session_factory=None,
        stage_log=[],
        solution_kind="unknown",
    )
    assert ok.intake is not None
    assert ok.intake.draft_brief.solution_kind == "digital"

    class _Broken:
        cost = type("Cost", (), {"by_provider": {}})()

        async def complete_json(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("отказ модели")

    fallback = await run_intake_stage(
        _Broken(),  # type: ignore[arg-type]
        "текст",
        industry_hint="н",
        session_factory=None,
        stage_log=[],
        solution_kind="business_model",
    )
    assert fallback.brief is not None
    assert fallback.brief.solution_kind == "business_model"


# ── форма ─────────────────────────────────────────────────────────────────────
def test_brief_from_form_читает_вид_решения_а_мусор_даёт_research():
    base = {"industry": "о", "task_summary": "с"}

    assert brief_store.brief_from_form({**base, "solution_kind": "digital"}).solution_kind == "digital"
    assert brief_store.brief_from_form({**base, "solution_kind": "unknown"}).solution_kind == "research"
    assert brief_store.brief_from_form(base).solution_kind == "research"


def test_форма_новой_задачи_предлагает_наука_по_умолчанию():
    html = (Path(__file__).resolve().parents[1] / "backend/app/web/templates/index.html").read_text(encoding="utf-8")

    assert 'name="solution_kind"' in html
    assert '<option value="research" selected>' in html


# ── хранение ──────────────────────────────────────────────────────────────────
@pytest.fixture
async def engine(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'solution-kind.db'}", future=True)
    yield engine
    await engine.dispose()


def _factory(engine):
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session
            await session.commit()

    return factory


@pytest.mark.parametrize("kind", KINDS)
async def test_круг_save_brief_load_brief(engine, brief: Brief, kind: str):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = _factory(engine)
    async with factory() as session:
        row = RequestRow(raw_text="задача", status="intake")
        session.add(row)
        await session.flush()
        request_id = row.id
        await brief_store.save_brief(
            session, request_id, brief.model_copy(update={"solution_kind": kind}), confirmed=False
        )

    async with factory() as session:
        loaded = await brief_store.load_brief(session, request_id)

    assert loaded.solution_kind == kind


async def test_база_без_колонки_догоняется_и_читает_research(engine, brief: Brief):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE briefs DROP COLUMN solution_kind"))

    async with engine.begin() as conn:
        added = await conn.run_sync(add_missing_columns)
    assert "briefs.solution_kind" in added

    factory = _factory(engine)
    async with factory() as session:
        row = RequestRow(raw_text="задача", status="intake")
        session.add(row)
        await session.flush()
        request_id = row.id
        saved = await brief_store.save_brief(session, request_id, brief, confirmed=False)
        saved.solution_kind = None  # строка, записанная до Э6-1
    async with factory() as session:
        assert (await brief_store.load_brief(session, request_id)).solution_kind == "research"
