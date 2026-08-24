"""Запуск прогона, замок и хаб запроса (`web/routes/run.py`, задача И-11).

Пайплайн подменён заглушкой `_FakePipeline` — живой `ScoutPipeline.run` не
вызывается ни разу (инвариант №7, тест без сети). База — временный файл
SQLite (правило теста, тот же приём, что `tests/test_persistence.py` и
фикстура `temp_db` в `tests/test_cli_run.py`): `app.db.session_scope`
подменяется, поэтому и обработчики через `Depends(get_session)`, и фоновая
задача (берёт сессию отложенным импортом `from app.db import session_scope`,
РЕШ-189 п. 10) видят одну и ту же временную базу.

Заглушка блокируется на `asyncio.Event` (`_FakePipeline.gate`) — так тест
детерминированно застаёт фоновую задачу «в процессе» без реальных пауз и без
гонки с ASGI-транспортом (инвариант №7): открывать замок или снимать его тест
решает явно, а не угадывает по времени.
"""

from __future__ import annotations

import asyncio
import itertools
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.schemas import Brief
from app.web import brief_store
from app.web.routes import run

_AUTH = (main.settings.web_auth_user, main.settings.web_auth_password.get_secret_value())
_ids = itertools.count(70_000)


def _next_raw_text() -> str:
    return f"тестовая задача №{next(_ids)}"


class _FakePipeline:
    """Заглушка `ScoutPipeline`: считает вызовы, ждёт управляемый gate,
    сама пишет статус в БД — тем же контрактом, что настоящий пайплайн
    (успех → `drafted`, отказ → `failed` до повторного `raise`). Стоимость
    складывается с уже стоящей в строке (РЕШ-193 п. 1, П-1 аудита Э3), а не
    перезаписывает её, — тем же выражением, что `persistence_write._write`
    на ветке обновления существующей строки."""

    calls = 0
    gate: asyncio.Event | None = None
    fail_message: str | None = None

    def __init__(self, *, session_factory=None, progress=None, **_ignored: Any) -> None:
        self._session_factory = session_factory
        self._progress = progress

    async def run(self, brief: Brief, *, request_id: int = 0, **_ignored: Any):
        type(self).calls += 1
        if self._progress:
            self._progress("planner", "тестовый прогон", 0.1)
        if type(self).fail_message is not None:
            message = type(self).fail_message
            async with self._session_factory() as session:
                row = await session.get(RequestRow, request_id)
                row.status = RequestStatus.FAILED
                row.error = message
            raise RuntimeError(message)
        if type(self).gate is not None:
            await type(self).gate.wait()
        async with self._session_factory() as session:
            row = await session.get(RequestRow, request_id)
            row.status = RequestStatus.DRAFTED
            row.cost_rub = (row.cost_rub or 0.0) + 4.56
        if self._progress:
            self._progress("done", "Отчёт готов. Стоимость запроса: 4.56 ₽", 1.0)
        return None


@pytest.fixture(autouse=True)
def _fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakePipeline.calls = 0
    _FakePipeline.gate = asyncio.Event()
    _FakePipeline.fail_message = None
    monkeypatch.setattr(run, "ScoutPipeline", _FakePipeline)


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Реестр задач — модульный синглтон, общий на весь прогон тестов."""
    run._active_runs.clear()
    yield
    run._active_runs.clear()


@pytest.fixture(autouse=True)
def _fresh_start_lock() -> None:
    """`asyncio.Lock` привязывается к циклу событий первого использования
    (`_get_loop`), а у pytest-asyncio цикл — свой на каждый тест: без
    пересоздания второй тест падает `RuntimeError: bound to a different
    event loop`, поймано прогоном конкурентных тестов ревью цикла 1."""
    run._start_lock = asyncio.Lock()


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Временная база вместо `data/scout.db` (см. докстроку модуля)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'web-run-test.db'}", future=True)
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


async def _make_request(db, *, status: RequestStatus = RequestStatus.BRIEFING, confirmed: bool = True) -> int:
    """Запрос вместе с брифом (подтверждённым по умолчанию)."""
    async with db() as session:
        row = RequestRow(raw_text=_next_raw_text(), status=status)
        session.add(row)
        await session.flush()
        request_id = int(row.id)
        brief = Brief(industry="нефтепереработка", task_summary="снизить износ насосного оборудования")
        await brief_store.save_brief(session, request_id, brief, confirmed=confirmed)
    return request_id


async def _row_status(db, request_id: int) -> RequestRow:
    async with db() as session:
        return await session.get(RequestRow, request_id)


# ── Критерий 1: старт, замок, завершение, отказ, плашка, стоимость ──────────


async def test_start_changes_status_and_creates_one_task(client, db):
    request_id = await _make_request(db)

    response = await client.post(f"/requests/{request_id}/start", auth=_AUTH)

    assert response.status_code == 200
    assert len(run._active_runs) == 1
    row = await _row_status(db, request_id)
    assert row.status == RequestStatus.SEARCHING


async def test_repeated_start_same_request_reuses_task(client, db):
    request_id = await _make_request(db)

    first = await client.post(f"/requests/{request_id}/start", auth=_AUTH)
    second = await client.post(f"/requests/{request_id}/start", auth=_AUTH)

    assert first.status_code == second.status_code == 200
    assert _FakePipeline.calls == 1
    assert len(run._active_runs) == 1

    _FakePipeline.gate.set()
    await run._active_runs[request_id]


async def test_start_other_request_refuses_while_one_active(client, db):
    active_id = await _make_request(db)
    other_id = await _make_request(db)

    await client.post(f"/requests/{active_id}/start", auth=_AUTH)
    response = await client.post(f"/requests/{other_id}/start", auth=_AUTH)

    assert response.status_code == 409
    assert "другой запрос" in response.text.lower()
    other_row = await _row_status(db, other_id)
    assert other_row.status == RequestStatus.BRIEFING
    assert other_id not in run._active_runs

    _FakePipeline.gate.set()
    await run._active_runs[active_id]


async def test_completion_writes_report_to_same_request_id(client, db):
    """Стоимость прогона складывается с уже потраченным по запросу до старта
    (интейк, сбор брифа — РЕШ-193 п. 1, П-1 аудита Э3), а не перезаписывает
    его: строка стартует с 0.18 ₽, прогон приносит 4.56 ₽ — итог 4.74 ₽."""
    request_id = await _make_request(db)
    async with db() as session:
        row = await session.get(RequestRow, request_id)
        row.cost_rub = 0.18

    await client.post(f"/requests/{request_id}/start", auth=_AUTH)

    _FakePipeline.gate.set()
    await run._active_runs[request_id]

    row = await _row_status(db, request_id)
    assert row.status == RequestStatus.DRAFTED
    assert row.cost_rub == pytest.approx(4.74)
    assert request_id not in run._active_runs
    async with db() as session:
        from sqlalchemy import func, select

        count = (await session.execute(select(func.count()).select_from(RequestRow))).scalar_one()
    assert count == 1


async def test_pipeline_failure_sets_failed_status_and_error(client, db):
    _FakePipeline.fail_message = "тестовый отказ пайплайна"
    request_id = await _make_request(db)
    await client.post(f"/requests/{request_id}/start", auth=_AUTH)

    await run._active_runs[request_id]

    row = await _row_status(db, request_id)
    assert row.status == RequestStatus.FAILED
    assert row.error == "тестовый отказ пайплайна"
    assert request_id not in run._active_runs


async def test_hub_shows_banner_when_searching_without_registered_task(client, db):
    """Перезапуск процесса теряет реестр — статус `searching` остаётся в БД."""
    request_id = await _make_request(db, status=RequestStatus.SEARCHING)

    response = await client.get(f"/requests/{request_id}", auth=_AUTH)

    assert response.status_code == 200
    assert "прогон прерван перезапуском" in response.text.lower()
    assert "запустить заново" in response.text.lower()


async def test_start_without_confirmed_brief_refuses(client, db):
    request_id = await _make_request(db, confirmed=False)

    response = await client.post(f"/requests/{request_id}/start", auth=_AUTH)

    assert response.status_code == 400
    assert "не подтверждён" in response.text.lower()
    row = await _row_status(db, request_id)
    assert row.status == RequestStatus.BRIEFING
    assert request_id not in run._active_runs


async def test_start_on_drafted_request_refuses_without_running_pipeline(client, db):
    """П-3 аудита Э3: запрос со статусом `drafted` — отчёт уже готов, второй
    платный прогон не заводится, заглушка пайплайна не запускается."""
    request_id = await _make_request(db, status=RequestStatus.DRAFTED)

    response = await client.post(f"/requests/{request_id}/start", auth=_AUTH)

    assert response.status_code == 409
    assert "уже готов" in response.text.lower()
    assert f"/requests/{request_id}/report" in response.text
    assert _FakePipeline.calls == 0
    assert request_id not in run._active_runs


async def test_hub_shows_cost_and_report_link_when_drafted(client, db):
    request_id = await _make_request(db, status=RequestStatus.DRAFTED)
    async with db() as session:
        row = await session.get(RequestRow, request_id)
        row.cost_rub = 12.34

    response = await client.get(f"/requests/{request_id}", auth=_AUTH)

    assert response.status_code == 200
    assert "12.34" in response.text or "12,34" in response.text
    assert f"/requests/{request_id}/report" in response.text


async def test_hub_not_found_returns_html_not_json(client, db):
    """Ревью цикла 1, находка 2: голый JSON `HTTPException` — не выход."""
    response = await client.get("/requests/999999", auth=_AUTH)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "не найден" in response.text.lower()


def _slow_load_brief(delay: float):
    """Обёртка над настоящим `brief_store.load_brief` с искусственной
    задержкой — раздвигает окно гонки так, чтобы два конкурентных запроса
    гарантированно оказались внутри него одновременно (ревью цикла 1,
    находка 1), а не полагаться на удачное совпадение по времени."""
    original = brief_store.load_brief

    async def slow(session, request_id):
        await asyncio.sleep(delay)
        return await original(session, request_id)

    return slow


# ── Ревью цикла 1, находка 1: гонка двойного клика ───────────────────────────


async def test_concurrent_double_click_same_request_runs_pipeline_once(client, db, monkeypatch: pytest.MonkeyPatch):
    """Два одновременных POST /start на один request_id — пайплайн запущен
    ровно один раз (замок атомарен, `_start_lock`)."""
    request_id = await _make_request(db)
    monkeypatch.setattr(brief_store, "load_brief", _slow_load_brief(0.05))

    first, second = await asyncio.gather(
        client.post(f"/requests/{request_id}/start", auth=_AUTH),
        client.post(f"/requests/{request_id}/start", auth=_AUTH),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert _FakePipeline.calls == 1
    assert len(run._active_runs) == 1

    _FakePipeline.gate.set()
    await run._active_runs[request_id]

    row = await _row_status(db, request_id)
    assert row.status == RequestStatus.DRAFTED


async def test_concurrent_start_different_requests_only_one_wins(client, db, monkeypatch: pytest.MonkeyPatch):
    """Два разных запроса стартуют одновременно — активным остаётся ровно
    один, второй отказывает по замку (РЕШ-147), а не проскакивает гонкой."""
    first_id = await _make_request(db)
    second_id = await _make_request(db)
    monkeypatch.setattr(brief_store, "load_brief", _slow_load_brief(0.05))

    first_response, second_response = await asyncio.gather(
        client.post(f"/requests/{first_id}/start", auth=_AUTH),
        client.post(f"/requests/{second_id}/start", auth=_AUTH),
    )

    statuses = sorted([first_response.status_code, second_response.status_code])
    assert statuses == [200, 409]
    assert _FakePipeline.calls == 1
    assert len(run._active_runs) == 1

    winner_id = first_id if first_response.status_code == 200 else second_id
    loser_id = second_id if winner_id == first_id else first_id
    loser_row = await _row_status(db, loser_id)
    assert loser_row.status == RequestStatus.BRIEFING
    assert loser_id not in run._active_runs

    _FakePipeline.gate.set()
    await run._active_runs[winner_id]
