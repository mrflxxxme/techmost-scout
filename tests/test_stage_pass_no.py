"""Проход поиска у записи журнала стадий (Ц-2 пост-аудита Э2-Б, РЕШ-82).

ЗАЧЕМ
    Владелец потребовал доклад о расходе после каждого платного прогона
    (РЕШ-82). Второй проход (РЕШ-81) даёт вторую строку `search` и вторую
    `scorer` с теми же именами стадий, и до этой правки ответ «сколько стоил
    второй проход» строился так: упорядочить строки по времени и отсечь всё
    после строки `replan`. Это вывод по эвристике порядка — ровно тот, от
    которого Т-19 сознательно ушла у планов («`pass_no` едет рядом с планом, а
    не выводится из порядка записей») и Т-20 у кандидатов.

    Здесь проверяется, что номер прохода едет своим полем и доезжает до
    `pipeline_logs`, а расход прохода считается фильтром по полю, без
    упорядочивания строк.

База — временный файл SQLite (`tmp_path`), сети нет: `persist_run` вызывается
напрямую, без пайплайна и без провайдеров моделей.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, PipelineLog
from app.pipeline.persistence import StageRecord, persist_run


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stage-pass-test.db'}", future=True)
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


def _stage(name: str, *, pass_no: int, cost_rub: float) -> StageRecord:
    moment = datetime.now(UTC)
    return StageRecord(
        stage=name,
        started_at=moment,
        finished_at=moment,
        pass_no=pass_no,
        provider="deepseek",
        cost_rub=cost_rub,
    )


async def test_записи_стадий_второго_прохода_помечены_вторым_проходом(db_session_factory):
    """Строки `search` и `scorer` второго прохода несут `pass_no = 2`, первого — 1."""
    stages = [
        _stage("search", pass_no=1, cost_rub=1.0),
        _stage("scorer", pass_no=1, cost_rub=2.0),
        _stage("replan", pass_no=2, cost_rub=0.5),
        _stage("search", pass_no=2, cost_rub=4.0),
        _stage("scorer", pass_no=2, cost_rub=8.0),
    ]
    await persist_run(
        db_session_factory,
        raw_text="прогон с двумя проходами поиска",
        status="drafted",
        cost_rub=15.5,
        cards=[],
        contest_cards=[],
        stages=stages,
    )

    async with db_session_factory() as session:
        rows = (await session.execute(select(PipelineLog))).scalars().all()

    by_pass = {(row.stage, row.pass_no) for row in rows}
    assert ("search", 1) in by_pass and ("search", 2) in by_pass, "проходы поиска в журнале неразличимы"
    assert ("scorer", 1) in by_pass and ("scorer", 2) in by_pass


async def test_расход_второго_прохода_считается_фильтром_а_не_порядком_строк(db_session_factory):
    """Ответ «сколько стоил второй проход» — сумма по полю, без сортировки по времени.

    Строки нарочно записаны вперемешку: если бы номер прохода по-прежнему
    выводился из порядка, такой журнал дал бы неверный ответ.
    """
    stages = [
        _stage("search", pass_no=2, cost_rub=4.0),
        _stage("search", pass_no=1, cost_rub=1.0),
        _stage("scorer", pass_no=2, cost_rub=8.0),
        _stage("scorer", pass_no=1, cost_rub=2.0),
    ]
    await persist_run(
        db_session_factory,
        raw_text="прогон, где строки журнала легли не по порядку проходов",
        status="drafted",
        cost_rub=15.0,
        cards=[],
        contest_cards=[],
        stages=stages,
    )

    async with db_session_factory() as session:
        rows = (await session.execute(select(PipelineLog))).scalars().all()

    assert sum(row.cost_rub for row in rows if row.pass_no == 2) == pytest.approx(12.0)
    assert sum(row.cost_rub for row in rows if row.pass_no == 1) == pytest.approx(3.0)


async def test_проход_по_умолчанию_первый(db_session_factory):
    """Стадия без явного номера прохода — первый проход, а не NULL и не ноль.

    Однопроходных стадий большинство (интейк, карточки, проверка, сводка), и
    заставлять каждое место записи проставлять единицу значило бы завести
    источник расхождений.
    """
    moment = datetime.now(UTC)
    await persist_run(
        db_session_factory,
        raw_text="однопроходный прогон",
        status="drafted",
        cost_rub=0.0,
        cards=[],
        contest_cards=[],
        stages=[StageRecord(stage="intake", started_at=moment, finished_at=moment)],
    )

    async with db_session_factory() as session:
        row = (await session.execute(select(PipelineLog))).scalars().one()

    assert row.pass_no == 1


def test_колонка_прохода_заведена_с_индексом():
    """Индекс заводится сразу вместе с колонкой: миграций в MVP нет.

    `init_db` — это `create_all`, он не добавляет ни колонок, ни индексов в уже
    существующую таблицу, поэтому «добавим индекс позже» означает ручной
    перенос базы (грабля Э2-А, тот же довод у `search_plans.pass_no`).
    """
    column = PipelineLog.__table__.c.pass_no
    assert column.index is True, "колонка прохода заведена без индекса — добавить его потом будет нечем"
    assert column.default is not None and column.default.arg == 1
