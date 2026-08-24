"""Тесты хранилища брифа (задача И-9): `brief_from_form`, `save_brief`,
`load_brief` — единственный путь, которым веб пишет и читает `schemas.Brief`.

Тесты не ходят в сеть (инвариант №7): база — временный файл SQLite на
`tmp_path`, отдельный от боевого `data/scout.db`, тот же приём, что в
`tests/test_persistence.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.models import Brief as BriefRow
from app.models import Request as RequestRow
from app.schemas import Brief, MaturityStage, OwnerAnswer
from app.web.brief_store import brief_from_form, load_brief, save_brief


@pytest.fixture
async def session(tmp_path: Path) -> AsyncSession:
    """Сессия на временном файле SQLite с уже созданными таблицами."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'brief-store-test.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_maker() as opened:
        yield opened
    await engine.dispose()


async def _make_request(session: AsyncSession, *, raw_text: str = "исходный текст задачи") -> int:
    """Строка `requests`, на которую ссылается `briefs.request_id` (внешний
    ключ), — фикстуры теста не подменяют его отдельной заглушкой."""
    row = RequestRow(raw_text=raw_text, status="briefing")
    session.add(row)
    await session.flush()
    return int(row.id)


def _sample_brief() -> Brief:
    return Brief(
        industry="нефтепереработка",
        task_summary="снизить износ насосного оборудования",
        constraints=["ГОСТ 12.2.007", "температура до 180°C"],
        owner_answers=[OwnerAnswer(question="Какая среда эксплуатации?", answer="агрессивная, с абразивом")],
        target_maturity=MaturityStage.PILOT,
        geography="РФ, Урал",
        budget_hint="до 5 млн ₽",
        timeline_hint="6 месяцев",
        keywords_ru=["насос", "износостойкость"],
        keywords_en=["pump", "wear resistance"],
    )


# ── Критерий 1: запись и чтение дают равный Brief
async def test_save_and_load_round_trip_equal_brief(session: AsyncSession):
    request_id = await _make_request(session)
    brief = _sample_brief()

    await save_brief(session, request_id, brief, confirmed=False)
    await session.commit()

    loaded = await load_brief(session, request_id)

    assert loaded is not None
    assert loaded.model_dump() == brief.model_dump()


# ── Критерий 1: повторная запись обновляет ту же строку
async def test_repeated_save_updates_same_row_not_duplicate(session: AsyncSession):
    request_id = await _make_request(session)
    first = _sample_brief()
    second = first.model_copy(update={"task_summary": "уточнённая формулировка задачи"})

    await save_brief(session, request_id, first, confirmed=False)
    await session.commit()
    await save_brief(session, request_id, second, confirmed=False)
    await session.commit()

    count = (await session.execute(select(func.count()).select_from(BriefRow))).scalar_one()
    assert count == 1

    loaded = await load_brief(session, request_id)
    assert loaded is not None
    assert loaded.task_summary == "уточнённая формулировка задачи"


# ── Критерий 1: confirmed=True проставляет confirmed_at
async def test_confirmed_true_sets_confirmed_at(session: AsyncSession):
    request_id = await _make_request(session)
    brief = _sample_brief()

    await save_brief(session, request_id, brief, confirmed=True)
    await session.commit()

    found = await session.execute(select(BriefRow).where(BriefRow.request_id == request_id))
    row = found.scalars().one()
    assert row.confirmed_at is not None


# ── Критерий 1: confirmed=False оставляет confirmed_at пустым
async def test_confirmed_false_leaves_confirmed_at_null(session: AsyncSession):
    request_id = await _make_request(session)
    brief = _sample_brief()

    await save_brief(session, request_id, brief, confirmed=False)
    await session.commit()

    found = await session.execute(select(BriefRow).where(BriefRow.request_id == request_id))
    row = found.scalars().one()
    assert row.confirmed_at is None


# ── Критерий 1: TRL из формы виден строкой в constraints
def test_trl_from_form_appears_as_string_in_constraints():
    fields = {
        "industry": "нефтепереработка",
        "task_summary": "снизить износ насосного оборудования",
        "constraints": "ГОСТ 12.2.007",
        "target_trl": "6",
    }

    brief = brief_from_form(fields)

    assert "целевой TRL 6" in brief.constraints
    assert "ГОСТ 12.2.007" in brief.constraints


# ── Критерий 1: пустой TRL строки не добавляет
def test_empty_trl_does_not_add_constraint_line():
    fields = {
        "industry": "нефтепереработка",
        "task_summary": "снизить износ насосного оборудования",
        "constraints": "ГОСТ 12.2.007",
        "target_trl": "",
    }

    brief = brief_from_form(fields)

    assert brief.constraints == ["ГОСТ 12.2.007"]
    assert not any("TRL" in line for line in brief.constraints)


# ── Критерий 1: метка {{...}} в любом поле формы приходит обезвреженной.
# Метка (`_PLACEHOLDER` в `app.pipeline.prompting`) — заглавные латинские буквы
# и подчёркивание, ровно то, что реально встречается в промптах.
def test_placeholder_marker_is_neutralized_in_every_field():
    fields = {
        "industry": "нефтепереработка {{CLIENT_NAME}}",
        "task_summary": "снизить износ {{CUSTOMER}} оборудования",
        "constraints": "требование {{REQUIREMENT}}\nвторая строка",
        "budget_hint": "{{BUDGET}}",
        "keywords_ru": "насос {{X}}",
    }

    brief = brief_from_form(fields)

    assert "{{" not in brief.industry
    assert "{{" not in brief.task_summary
    assert all("{{" not in line for line in brief.constraints)
    assert brief.budget_hint is not None
    assert "{{" not in brief.budget_hint
    assert all("{{" not in word for word in brief.keywords_ru)


# ── Критерий 1: load_brief для запроса без брифа возвращает None
async def test_load_brief_returns_none_when_absent(session: AsyncSession):
    request_id = await _make_request(session)

    loaded = await load_brief(session, request_id)

    assert loaded is None


# ── Дополнительно: поля формы = поля схемы (РЕШ-143 п. 4), список и строка
# textarea дают одинаковый результат для constraints/keywords
def test_form_list_field_accepts_both_string_and_list():
    from_string = brief_from_form(
        {
            "industry": "энергетика",
            "task_summary": "снизить теплопотери",
            "constraints": "требование А\nтребование Б",
        }
    )
    from_list = brief_from_form(
        {
            "industry": "энергетика",
            "task_summary": "снизить теплопотери",
            "constraints": ["требование А", "требование Б"],
        }
    )

    assert from_string.constraints == from_list.constraints == ["требование А", "требование Б"]


# ── Дополнительно: пустая форма не роняет сборку, поля пустые/умолчания
def test_brief_from_form_with_minimal_fields():
    brief = brief_from_form({"industry": "медицина", "task_summary": "суть задачи"})

    assert brief.industry == "медицина"
    assert brief.task_summary == "суть задачи"
    assert brief.constraints == []
    assert brief.owner_answers == []
    assert brief.target_maturity is None
    assert brief.geography == "РФ"
