"""Демо-режим: список кейсов и воспроизведение (Э4-И7, SPEC Э4-3, РЕШ-200/201).

Проверяется поверх настоящего пайплайна воспроизведения (`app.pipeline.demo`)
и настоящего брокера прогресса (`app.web.progress`) — подменены только база
(временный файл SQLite, тот же приём, что `tests/test_web_run.py`) и
ожидание между шагами (`demo._sleep`, инвариант №7: показ не должен ждать
настоящих секунд). `LLMRouter` в демо-пути не создаётся ни разу — критерий 3
проверяет это счётчиком подменённого класса, а не чтением исходников.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.models import Base, RequestStatus
from app.models import PipelineLog as PipelineLogRow
from app.models import Request as RequestRow
from app.web.routes import demo as demo_route

_AUTH = (main.settings.web_auth_user, main.settings.web_auth_password.get_secret_value())
_ids = itertools.count(80_000)


def _next_raw_text() -> str:
    return f"демо-задача №{next(_ids)}"


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Реестр активных показов — модульный синглтон, общий на весь прогон тестов."""
    demo_route._active_demos.clear()
    yield
    demo_route._active_demos.clear()


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Показ не ждёт настоящих секунд (инвариант №7) — вместо ожидания копим вызовы."""
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(demo_route, "_sleep", fake_sleep)
    return calls


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Временная база вместо `data/scout.db` (см. докстроку модуля)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'web-demo-test.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def factory():
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr("app.db.session_scope", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _make_request(db, *, demo_flag: bool = True, cost_rub: float = 0.0) -> int:
    """`id` задаётся явно большим числом (счётчик с 80 000): брокер прогресса
    — общий на процесс синглтон (`app.web.progress`, известный долг —
    каналы не освобождаются после `done`), а автоинкремент временной базы у
    каждого теста стартует заново с единицы. Без явного `id` демо-события
    этого файла оседали бы в канале request_id=1 и портили буфер другим
    тестам, честно проверяющим поток прогресса по маленьким id (найдено на
    практике: `test_web_flow_e2e.py` терял стадию `cards` из-за чужого
    буферизованного `done`)."""
    request_id = next(_ids)
    async with db() as session:
        row = RequestRow(
            id=request_id,
            raw_text=_next_raw_text(),
            status=RequestStatus.DRAFTED,
            demo_flag=demo_flag,
            cost_rub=cost_rub,
        )
        session.add(row)
        await session.flush()
        return int(row.id)


async def _add_logs(db, request_id: int, stages: list[tuple[str, float, float]]) -> None:
    """`stages` — тройки (имя стадии, смещение старта в секундах, длительность)."""
    base = datetime(2026, 8, 27, 9, 0, 0, tzinfo=UTC)
    async with db() as session:
        for stage, offset, duration in stages:
            started = base + timedelta(seconds=offset)
            session.add(
                PipelineLogRow(
                    request_id=request_id,
                    stage=stage,
                    started_at=started,
                    finished_at=started + timedelta(seconds=duration),
                    sources_hit={"crossref": 3} if stage == "search" else {},
                )
            )


async def _row(db, request_id: int) -> RequestRow:
    async with db() as session:
        return await session.get(RequestRow, request_id)


async def _await_demo_task(request_id: int) -> None:
    """Дожидается фоновой задачи показа, если она ещё не завершилась сама.

    Подменённый `_sleep` ничего по-настоящему не ждёт, поэтому задача нередко
    успевает выполниться и снять себя с реестра ещё до возврата из `POST`
    (обычный `await` без реальной приостановки не отдаёт цикл событий) —
    прямое обращение по ключу словаря тогда упало бы `KeyError`.
    """
    task = demo_route._active_demos.get(request_id)
    if task is not None:
        await task


async def _sse_events(client: AsyncClient, request_id: int) -> list[dict]:
    response = await client.get(f"/requests/{request_id}/progress", auth=_AUTH)
    lines = [line for line in response.text.splitlines() if line.startswith("data:")]
    return [json.loads(line[len("data:") :].strip()) for line in lines]


# ── Критерий 2: список только demo_flag=true ────────────────────────────────


async def test_demo_index_shows_only_demo_flag_requests(client, db):
    shown_id = await _make_request(db, demo_flag=True)
    hidden_id = await _make_request(db, demo_flag=False)

    response = await client.get("/demo", auth=_AUTH)

    assert response.status_code == 200
    assert f"/demo/{shown_id}/start" in response.text
    assert f"/demo/{hidden_id}/start" not in response.text


async def test_demo_index_renders_with_no_cases(client, db):
    response = await client.get("/demo", auth=_AUTH)

    assert response.status_code == 200
    assert "демо-кейсов пока нет" in response.text.lower()


# ── Критерий 2: старт публикует события, поток отдаёт их в нужном формате ────


async def test_start_publishes_events_stream_gets_stage_message_fraction(client, db):
    request_id = await _make_request(db)
    await _add_logs(db, request_id, [("planner", 0, 10), ("search", 10, 20), ("scorer", 30, 70)])

    response = await client.post(f"/demo/{request_id}/start", auth=_AUTH)
    assert response.status_code == 200

    await _await_demo_task(request_id)

    events = await _sse_events(client, request_id)

    assert events, "поток не отдал ни одного события"
    for event in events:
        assert {"stage", "message", "fraction"} <= set(event)
    assert events[-1]["stage"] == "done"
    assert events[-1]["fraction"] == 1.0
    assert "₽" not in events[-1]["message"]  # финал без суммы стоимости (РЕШ-188)
    assert "стоимост" not in events[-1]["message"].lower()


# ── Критерий 2: повторный старт активного показа второй задачи не заводит ────


async def test_repeated_start_of_active_show_reuses_task(client, db, monkeypatch: pytest.MonkeyPatch):
    request_id = await _make_request(db)
    await _add_logs(db, request_id, [("planner", 0, 10), ("search", 10, 20)])

    gate = asyncio.Event()

    async def blocking_sleep(_seconds: float) -> None:
        await gate.wait()

    monkeypatch.setattr(demo_route, "_sleep", blocking_sleep)

    first = await client.post(f"/demo/{request_id}/start", auth=_AUTH)
    second = await client.post(f"/demo/{request_id}/start", auth=_AUTH)

    assert first.status_code == second.status_code == 200
    assert len(demo_route._active_demos) == 1

    gate.set()
    await demo_route._active_demos[request_id]


# ── Критерий 3: ни рубля, ни одного вызова провайдера моделей ────────────────


async def test_playback_does_not_change_cost_and_does_not_touch_llm_router(client, db, monkeypatch: pytest.MonkeyPatch):
    request_id = await _make_request(db, cost_rub=12.34)
    await _add_logs(db, request_id, [("planner", 0, 10), ("search", 10, 20)])

    calls = {"count": 0}

    class _CountingRouter:
        def __init__(self, *args, **kwargs) -> None:
            calls["count"] += 1

    monkeypatch.setattr("app.llm.LLMRouter", _CountingRouter)

    await client.post(f"/demo/{request_id}/start", auth=_AUTH)
    await _await_demo_task(request_id)

    assert calls["count"] == 0
    row = await _row(db, request_id)
    assert row.cost_rub == pytest.approx(12.34)


# ── Инвариант №5: пустой журнал — честный отказ, страница не падает ──────────


async def test_start_with_empty_log_shows_honest_refusal(client, db):
    request_id = await _make_request(db)

    response = await client.post(f"/demo/{request_id}/start", auth=_AUTH)

    assert response.status_code == 409
    assert "журнал" in response.text.lower()
    assert request_id not in demo_route._active_demos


async def test_start_unknown_request_returns_not_found(client, db):
    response = await client.post("/demo/999999/start", auth=_AUTH)

    assert response.status_code == 404
    assert "не найден" in response.text.lower()
