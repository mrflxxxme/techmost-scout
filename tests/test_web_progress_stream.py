"""Поток прогресса `GET /requests/{id}/progress` (задача И-11, SPEC Э3-4).

Проверяется исключительно HTTP-слой сериализации в SSE поверх реального
брокера `app.web.progress` (И-2) — без базы и без пайплайна: маршрут читает
только брокер, поэтому запросу не обязательно существовать в БД. Каждый тест
берёт свой `request_id` (счётчик), чтобы буфер брокера — общий на процесс
синглтон — не смешивал события разных тестов (инвариант №7, тест без сети).

`httpx.ASGITransport` не умеет по-настоящему конкурентно читать
незавершённый поток — он целиком проигрывает вызов ASGI-приложения и только
потом отдаёт собранный ответ (проверено эмпирически: `client.stream(...)`
зависает, если генератор ни разу не отдаёт `done`). Поэтому каждый сценарий
устроен так, чтобы поток гарантированно завершился в ограниченное время:
либо публикуем `done` заранее, либо планируем его отложенной фоновой
задачей — событие «протухания» сработает раньше (интервал укорочен через
`run._KEEPALIVE_INTERVAL`, реального ожидания в 2 секунды нигде нет), и весь
`GET`-запрос читается целиком одним вызовом.
"""

from __future__ import annotations

import asyncio
import itertools
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.config import get_settings
from app.web import progress
from app.web.routes import run

_AUTH = (main.settings.web_auth_user, main.settings.web_auth_password.get_secret_value())
_ids = itertools.count(90_000)


def _next_id() -> int:
    return next(_ids)


@pytest.fixture(autouse=True)
def _short_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Короткий интервал keepalive — без реального ожидания в 2 секунды."""
    monkeypatch.setattr(run, "_KEEPALIVE_INTERVAL", 0.02)


@pytest.fixture
async def client():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _events(client: AsyncClient, request_id: int) -> tuple[int, list[dict]]:
    """GET на весь поток целиком (см. докстроку модуля) — статус и события."""
    response = await client.get(f"/requests/{request_id}/progress", auth=_AUTH)
    lines = [line for line in response.text.splitlines() if line.startswith("data:")]
    events = [json.loads(line[len("data:") :].strip()) for line in lines]
    return response.status_code, events


#: Ссылки на фоновые задачи `_publish_done_later` — без них `asyncio.create_task`
#: не держит объект живым, и планировщик вправе собрать его до срабатывания.
_background_tasks: set[asyncio.Task] = set()


def _publish_done_later(request_id: int, *, delay: float, message: str = "Прогон завершён.") -> None:
    """Публикует `done` фоновой задачей, чтобы поток гарантированно закрылся:
    к моменту публикации keepalive уже успеет сработать хотя бы раз."""

    async def _later() -> None:
        await asyncio.sleep(delay)
        progress.hook_for(request_id)("done", message, 1.0)

    task = asyncio.create_task(_later())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def test_content_type_is_event_stream(client: AsyncClient):
    request_id = _next_id()
    progress.hook_for(request_id)("done", "Отчёт готов. Стоимость запроса: 1.00 ₽", 1.0)
    response = await client.get(f"/requests/{request_id}/progress", auth=_AUTH)
    assert response.headers["content-type"].startswith("text/event-stream")


async def test_events_carry_stage_message_fraction(client: AsyncClient):
    request_id = _next_id()
    hook = progress.hook_for(request_id)
    hook("planner", "Составляю план поиска", 0.05)
    hook("done", "Отчёт готов. Стоимость запроса: 3.50 ₽", 1.0)

    _, events = await _events(client, request_id)

    assert events[0] == {"stage": "planner", "message": "Составляю план поиска", "fraction": 0.05}
    assert events[1]["stage"] == "done"
    assert events[1]["fraction"] == 1.0


async def test_unavailable_sources_message_reaches_stream_verbatim(client: AsyncClient):
    """Строка `run_stages.py:117` доезжает до потока дословно (инвариант №5)."""
    request_id = _next_id()
    hook = progress.hook_for(request_id)
    hook("search", "Не ответили: rospatent, openalex", 0.42)
    hook("done", "Отчёт готов. Стоимость запроса: 0.00 ₽", 1.0)

    _, events = await _events(client, request_id)

    assert events[0]["message"] == "Не ответили: rospatent, openalex"


async def test_keepalive_marks_repeated_event(client: AsyncClient):
    """Молчание источника даёт повтор последнего события с `keepalive: true`."""
    request_id = _next_id()
    progress.hook_for(request_id)("search", "Опрашиваю источники", 0.2)
    # done приходит позже одного тика keepalive (0.02 c) — минимум один повтор
    # успевает сработать раньше, чем поток закроется.
    _publish_done_later(request_id, delay=0.06)

    _, events = await _events(client, request_id)

    assert events[0]["stage"] == "search"
    assert events[0].get("keepalive") is None
    repeats = [e for e in events[1:-1] if e["stage"] == "search"]
    assert repeats, "ожидался хотя бы один повтор события с keepalive=true"
    assert all(e["keepalive"] is True for e in repeats)
    assert events[-1]["stage"] == "done"


async def test_done_event_closes_stream(client: AsyncClient):
    request_id = _next_id()
    progress.hook_for(request_id)("done", "Отчёт готов. Стоимость запроса: 2.00 ₽", 1.0)

    _, events = await _events(client, request_id)

    assert len(events) == 1
    assert events[0]["stage"] == "done"


async def test_demo_mode_hides_cost_but_keeps_ready_notice(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """РЕШ-188: в демо-режиме нет символа «₽», а «Отчёт готов» на месте."""
    monkeypatch.setattr(get_settings(), "demo_mode_default", True)
    request_id = _next_id()
    progress.hook_for(request_id)("done", "Отчёт готов. Стоимость запроса: 4.20 ₽", 1.0)

    _, events = await _events(client, request_id)

    assert "₽" not in events[0]["message"]
    assert "Отчёт готов" in events[0]["message"]


async def test_demo_mode_off_keeps_cost_visible(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "demo_mode_default", False)
    request_id = _next_id()
    progress.hook_for(request_id)("done", "Отчёт готов. Стоимость запроса: 4.20 ₽", 1.0)

    _, events = await _events(client, request_id)

    assert "₽" in events[0]["message"]
