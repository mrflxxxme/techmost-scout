"""Поля экосистемы в брифе: `region_code`, `need_id`, `mode` (План Э6-API, А1; РЕШ-250).

Проверяется: шаблоны значений, исключение полей из сериализации (бриф уходит
в промпты дампом), снятие полей с ответа модели, запись и чтение строки
`briefs`, догон колонок в существующей базе. База — временный файл SQLite,
сеть не задействована.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import ADDED_COLUMNS, add_missing_columns
from app.models import Base
from app.models import Request as RequestRow
from app.pipeline.prompting import render_prompt
from app.schemas import Brief, IntakeResult
from app.web import brief_store


@pytest.fixture
async def engine(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ecosystem-fields.db'}", future=True)
    yield engine
    await engine.dispose()


def _factory(engine):
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

    return factory


# ── Шаблоны значений ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("code", ["83", "86", "89", "77", "01"])
def test_region_code_две_цифры_принимается(brief: Brief, code: str):
    assert Brief(**brief.model_dump(), region_code=code).region_code == code


@pytest.mark.parametrize("code", ["8", "123", "8a", "ab", "٨٣", "83\n", " 83", ""])
def test_region_code_не_две_цифры_отклоняется(brief: Brief, code: str):
    """Ровно две ASCII-цифры: арабские цифры, пробел и перевод строки — ошибка валидации."""
    with pytest.raises(ValidationError):
        Brief(**brief.model_dump(), region_code=code)


@pytest.mark.parametrize("need_id", ["restaurants_cafes", "ab", "a1_b2"])
def test_need_id_по_шаблону_принимается(brief: Brief, need_id: str):
    assert Brief(**brief.model_dump(), need_id=need_id).need_id == need_id


@pytest.mark.parametrize("need_id", ["a", "Restaurants", "1abc", "rest-cafes", "a" * 65, "кафе"])
def test_need_id_не_по_шаблону_отклоняется(brief: Brief, need_id: str):
    with pytest.raises(ValidationError):
        Brief(**brief.model_dump(), need_id=need_id)


def test_mode_по_умолчанию_business_и_только_два_значения(brief: Brief):
    assert brief.mode == "business"
    assert Brief(**brief.model_dump(), mode="gov").mode == "gov"
    with pytest.raises(ValidationError):
        Brief(**brief.model_dump(), mode="world")


# ── Поля не попадают в промпты ────────────────────────────────────────────────
def test_поля_экосистемы_не_попадают_в_дамп_брифа(brief: Brief):
    """Бриф уходит в промпт дампом `model_dump_json` (`BRIEF_JSON`): полей там нет."""
    marked = brief.model_copy(update={"region_code": "86", "need_id": "restaurants_cafes", "mode": "gov"})

    assert marked.model_dump_json(indent=2) == brief.model_dump_json(indent=2)
    for name in ("region_code", "need_id", "mode", "restaurants_cafes"):
        assert name not in marked.model_dump_json()


def test_промпт_планировщика_не_меняется_от_полей_экосистемы(brief: Brief):
    marked = brief.model_copy(update={"region_code": "86", "need_id": "restaurants_cafes", "mode": "gov"})

    plain = render_prompt("planner", BRIEF_JSON=brief.model_dump_json(indent=2), SOLUTION_KIND=brief.solution_kind)
    with_fields = render_prompt(
        "planner", BRIEF_JSON=marked.model_dump_json(indent=2), SOLUTION_KIND=marked.solution_kind
    )

    assert with_fields == plain


def test_промпты_не_называют_поля_экосистемы():
    """Меток и упоминаний полей в `prompts/*.md` нет: модель их не заполняет."""
    prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
    for path in prompts_dir.glob("*.md"):
        if path.name == "CHANGELOG.md":
            continue
        content = path.read_text(encoding="utf-8")
        for name in ("region_code", "need_id", "REGION_CODE", "NEED_ID"):
            assert name not in content, f"{path.name} упоминает {name}"


def test_ответ_модели_с_полями_экосистемы_их_не_ставит(brief: Brief):
    """Модель вернула `region_code` по своей догадке — в бриф он не попадает."""
    payload = {
        "questions": [],
        "draft_brief": {**brief.model_dump(mode="json"), "region_code": "77", "need_id": "guess_id", "mode": "gov"},
    }

    result = IntakeResult.model_validate(payload)

    assert result.draft_brief.region_code is None
    assert result.draft_brief.need_id is None
    assert result.draft_brief.mode == "business"


def test_ответ_модели_с_испорченным_region_code_не_валит_разбор(brief: Brief):
    """Снятие идёт до валидации: мусорное значение от модели не роняет интейк."""
    payload = {"questions": [], "draft_brief": {**brief.model_dump(mode="json"), "region_code": "Москва"}}

    assert IntakeResult.model_validate_json(json.dumps(payload)).draft_brief.region_code is None


# ── Запись и чтение строки briefs ─────────────────────────────────────────────
async def test_save_и_load_brief_сохраняют_поля_экосистемы(engine, brief: Brief):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = _factory(engine)
    marked = brief.model_copy(update={"region_code": "89", "need_id": "restaurants_cafes", "mode": "gov"})

    async with factory() as session:
        row = RequestRow(raw_text="задача", status="intake")
        session.add(row)
        await session.flush()
        request_id = row.id
        await brief_store.save_brief(session, request_id, marked, confirmed=False)

    async with factory() as session:
        loaded = await brief_store.load_brief(session, request_id)

    assert (loaded.region_code, loaded.need_id, loaded.mode) == ("89", "restaurants_cafes", "gov")


async def test_строка_без_mode_читается_как_business(engine, brief: Brief):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = _factory(engine)
    async with factory() as session:
        row = RequestRow(raw_text="задача", status="intake")
        session.add(row)
        await session.flush()
        request_id = row.id
        saved = await brief_store.save_brief(session, request_id, brief, confirmed=False)
        saved.mode = None

    async with factory() as session:
        loaded = await brief_store.load_brief(session, request_id)

    assert loaded.mode == "business"
    assert loaded.region_code is None


# ── Догон схемы существующей базы ─────────────────────────────────────────────
async def test_существующая_база_без_новых_колонок_получает_их(engine, brief: Brief):
    """База, созданная до РЕШ-250, открывается и получает колонки тем же путём, что `init_db`."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table_name, column_name in ADDED_COLUMNS:
            await conn.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))
        await conn.execute(
            text(
                "INSERT INTO requests (created_at, raw_text, status, cost_rub, demo_flag) "
                "VALUES ('2026-01-01 00:00:00', 'старая', 'drafted', 0, 0)"
            )
        )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(add_missing_columns)
        again = await conn.run_sync(add_missing_columns)

    assert sorted(added) == sorted(f"{table}.{column}" for table, column in ADDED_COLUMNS)
    assert again == [], "повторный догон ничего не дописывает"
    async with engine.connect() as conn:
        columns = await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns("briefs")})
    assert {"region_code", "need_id", "mode"} <= columns

    factory = _factory(engine)
    async with factory() as session:
        old = (await session.execute(text("SELECT id FROM requests WHERE raw_text = 'старая'"))).scalar_one()
        assert (await session.get(RequestRow, old)).api_client is None
        marked = brief.model_copy(update={"region_code": "83"})
        await brief_store.save_brief(session, old, marked, confirmed=False)
    async with factory() as session:
        assert (await brief_store.load_brief(session, old)).region_code == "83"


async def test_init_db_зовёт_догон_колонок(monkeypatch: pytest.MonkeyPatch, engine):
    """`init_db` — единственная точка, через которую боевая база получает колонки."""
    import app.db as db_module

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE requests DROP COLUMN api_client"))
    monkeypatch.setattr(db_module, "engine", engine)

    await db_module.init_db()

    async with engine.connect() as conn:
        columns = await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns("requests")})
    assert "api_client" in columns
