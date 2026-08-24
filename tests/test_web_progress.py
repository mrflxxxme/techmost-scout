"""Брокер прогресса прогона (`app.web.progress`, РЕШ-183).

Модуль работает целиком в памяти процесса — сети и БД в тестах нет
(инвариант №7). Молчание источника проверяется коротким параметром
`keepalive_interval`, реальных ожиданий по 2 секунды тесты не делают.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.web.progress import ProgressBroker


async def test_subscriber_receives_events_in_order():
    """Подписчик получает опубликованные события ровно в порядке публикации."""
    broker = ProgressBroker()
    hook = broker.hook_for(1)
    hook("planner", "составляю план", 0.1)
    hook("search", "опрашиваю источники", 0.4)
    hook("done", "готово", 1.0)

    events = [event async for event in broker.subscribe(1, keepalive_interval=0.05)]

    assert [(e.stage, e.fraction) for e in events] == [
        ("planner", 0.1),
        ("search", 0.4),
        ("done", 1.0),
    ]


async def test_late_subscriber_sees_buffered_events():
    """Подписавшийся после публикации всё равно видит уже случившиеся события."""
    broker = ProgressBroker()
    hook = broker.hook_for(2)
    hook("planner", "составляю план", 0.1)
    hook("scorer", "оцениваю кандидатов", 0.6)
    hook("done", "готово", 1.0)

    events = [event async for event in broker.subscribe(2, keepalive_interval=0.05)]

    assert events[0].stage == "planner"
    assert events[1].stage == "scorer"
    assert events[-1].stage == "done"


async def test_keepalive_on_silence():
    """При молчании источника подписчик получает повтор последнего события."""
    broker = ProgressBroker()
    hook = broker.hook_for(3)
    hook("search", "опрашиваю источники", 0.4)

    gen = broker.subscribe(3, keepalive_interval=0.01)
    try:
        first = await gen.__anext__()
        assert first.stage == "search"
        # события больше не публикуются — второй элемент обязан прийти по
        # тайм-ауту keepalive, а не зависнуть
        keepalive = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert keepalive.stage == "search"
        assert keepalive.message == "опрашиваю источники"
    finally:
        await gen.aclose()


async def test_done_event_closes_stream():
    """Событие `done` завершает генератор — дальше `async for` не блокируется."""
    broker = ProgressBroker()
    hook = broker.hook_for(4)
    hook("summary", "собираю сводку", 0.9)
    hook("done", "готово", 1.0)

    # Если бы генератор не закрывался сам, сбор списка ниже завис бы на
    # ожидании следующего события — тест ограничен таймаутом, чтобы это было
    # видно как падение, а не зависание набора тестов.
    async def collect() -> list[str]:
        return [event.stage async for event in broker.subscribe(4, keepalive_interval=0.05)]

    events = await asyncio.wait_for(collect(), timeout=1.0)
    assert events == ["summary", "done"]


async def test_events_of_different_requests_do_not_mix():
    """События двух прогонов идут в раздельные каналы и не пересекаются."""
    broker = ProgressBroker()
    hook_a = broker.hook_for(10)
    hook_b = broker.hook_for(20)
    hook_a("planner", "план А", 0.1)
    hook_b("planner", "план Б", 0.1)
    hook_a("done", "готово А", 1.0)
    hook_b("done", "готово Б", 1.0)

    events_a = [event.message async for event in broker.subscribe(10, keepalive_interval=0.05)]
    events_b = [event.message async for event in broker.subscribe(20, keepalive_interval=0.05)]

    assert events_a == ["план А", "готово А"]
    assert events_b == ["план Б", "готово Б"]


async def test_publish_survives_disconnected_subscriber():
    """Отключение подписчика (закрытие генератора) не роняет публикацию (№5)."""
    broker = ProgressBroker()
    request_id = 30
    hook = broker.hook_for(request_id)
    hook("planner", "старт", 0.1)

    gen = broker.subscribe(request_id, keepalive_interval=5.0)
    first = await gen.__anext__()
    assert first.stage == "planner"
    await gen.aclose()  # клиент закрыл вкладку раньше события done

    # публикация после отключения подписчика не должна поднимать исключение
    hook("search", "после отключения", 0.4)

    channel = broker._channel(request_id)
    assert channel.subscribers == []


async def test_publish_does_not_raise_when_subscriber_queue_is_full():
    """Переполнение очереди медленного подписчика не роняет публикацию (№5)."""
    broker = ProgressBroker()
    request_id = 40
    gen = broker.subscribe(request_id, keepalive_interval=5.0)
    # запускаем генератор до первой точки ожидания, чтобы очередь
    # зарегистрировалась, но подписчик ничего не забирал
    pending = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)

    hook = broker.hook_for(request_id)
    for i in range(300):
        hook("search", f"событие {i}", 0.5)

    pending.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pending
    await gen.aclose()


def test_ring_buffer_overflow_evicts_oldest_without_raising():
    """Переполнение общего буфера запроса не роняет публикацию (№5)."""
    broker = ProgressBroker()
    request_id = 50
    hook = broker.hook_for(request_id)
    for i in range(500):
        hook("search", f"событие {i}", 0.5)

    channel = broker._channel(request_id)
    assert len(channel.buffer) == 200
    assert channel.buffer[0].message == "событие 300"
    assert channel.buffer[-1].message == "событие 499"


@pytest.mark.parametrize("request_id", [1, 2])
def test_hook_for_returns_callable_matching_progress_hook(request_id: int):
    """`hook_for` отдаёт синхронный вызываемый объект с сигнатурой `ProgressHook`."""
    broker = ProgressBroker()
    hook = broker.hook_for(request_id)
    result = hook("planner", "проверка сигнатуры", 0.2)
    assert result is None
    channel = broker._channel(request_id)
    assert channel.buffer[-1].stage == "planner"
