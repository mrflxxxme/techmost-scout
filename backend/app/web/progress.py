"""Брокер событий прогресса прогона — в памяти процесса (РЕШ-183).

Развилка В1 грилла этапа 3: события прогресса не пишутся в БД по ходу
прогона — там по-прежнему меняется только `requests.status`, а
`pipeline_logs` ложится одной транзакцией в конце (`persist_run`, И-5). Этот
модуль отвечает только за доставку событий живым подписчикам SSE-маршрута
(HTTP-часть — задача И-11, здесь её нет): кольцевой буфер последних событий
на запрос плюс очередь на подписчика.

Модуль не знает о слоях работы с базой (модуль подключения, ORM-модели, сама
СУБД-библиотека) и ничего в базу не пишет — рестарт процесса теряет только
текущий прогресс, а не сам прогон.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

#: Сколько последних событий на запрос хранится для подписчиков, пришедших
#: позже публикации (демо-сценарий Э3-4: открыли вкладку прогресса не сразу).
_BUFFER_SIZE = 200

#: Очередь на подписчика: чтобы медленный клиент не блокировал публикацию
#: (инвариант №5), лишние события вытесняются, а не копятся бесконечно.
_QUEUE_SIZE = 200


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Одно событие прогресса — то, что уходит в SSE-поток."""

    stage: str
    message: str
    fraction: float


@dataclass(slots=True)
class _RequestChannel:
    """Буфер и подписчики одного запроса."""

    buffer: deque[ProgressEvent] = field(default_factory=lambda: deque(maxlen=_BUFFER_SIZE))
    subscribers: list[asyncio.Queue[ProgressEvent]] = field(default_factory=list)


class ProgressBroker:
    """Публикация и подписка на события прогресса, отдельно по `request_id`."""

    def __init__(self) -> None:
        self._channels: dict[int, _RequestChannel] = {}

    def _channel(self, request_id: int) -> _RequestChannel:
        channel = self._channels.get(request_id)
        if channel is None:
            channel = _RequestChannel()
            self._channels[request_id] = channel
        return channel

    def publish(self, request_id: int, stage: str, message: str, fraction: float) -> None:
        """Кладёт событие в буфер запроса и рассылает его живым подписчикам.

        Переполнение очереди подписчика гасится молча (инвариант №5): один
        отставший клиент не должен ронять публикацию ни для остальных
        подписчиков, ни для самого пайплайна.
        """
        event = ProgressEvent(stage=stage, message=message, fraction=fraction)
        channel = self._channel(request_id)
        channel.buffer.append(event)
        for queue in channel.subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    def hook_for(self, request_id: int) -> Callable[[str, str, float], None]:
        """Отдаёт `ProgressHook` (см. `app.pipeline.pipeline_context`) для
        публикации событий указанного прогона — сигнатура `(stage, message,
        fraction)`, вызов синхронный: пайпларну подходит любой из вариантов
        `Callable[[str, str, float], Awaitable[None] | None]`.
        """

        def hook(stage: str, message: str, fraction: float) -> None:
            self.publish(request_id, stage, message, fraction)

        return hook

    async def subscribe(self, request_id: int, *, keepalive_interval: float = 2.0) -> AsyncIterator[ProgressEvent]:
        """Асинхронный генератор событий запроса — сперва буфер, затем новые.

        При молчании источника дольше `keepalive_interval` секунд отдаёт
        последнее известное событие повторно (keepalive), чтобы клиент не
        решил, что поток завис (SPEC Э3-4: не реже раза в 2 секунды).
        Интервал параметризован ради тестов — реальных ожиданий там нет.
        Поток закрывается сам после события `done` (или `fraction >= 1.0`):
        вызывающему коду останавливать `async for` не нужно.
        """
        channel = self._channel(request_id)
        queue: asyncio.Queue[ProgressEvent] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        for buffered in list(channel.buffer):
            queue.put_nowait(buffered)
        channel.subscribers.append(queue)
        last_event: ProgressEvent | None = None
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=keepalive_interval)
                except TimeoutError:
                    if last_event is not None:
                        yield last_event
                    continue
                last_event = event
                yield event
                if event.stage == "done" or event.fraction >= 1.0:
                    return
        finally:
            if queue in channel.subscribers:
                channel.subscribers.remove(queue)


#: Единственный на процесс брокер — прогонов немного, отдельный DI не нужен
#: (тот же приём, что у `app.llm.get_router`/`get_settings`).
_broker = ProgressBroker()


def get_broker() -> ProgressBroker:
    """Возвращает общий на процесс брокер прогресса."""
    return _broker


def hook_for(request_id: int) -> Callable[[str, str, float], None]:
    """Короткий путь для оркестратора: `progress.hook_for(request_id)`."""
    return _broker.hook_for(request_id)
