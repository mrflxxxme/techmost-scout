"""Доступ приложений экосистемы к `/api/v1`: ключи и ограничение частоты (РЕШ-250).

Ключи живут только в окружении (`SCOUT_API_KEYS=имя:ключ,имя2:ключ2`), в
коде и в базе их нет. Имя приложения — то, что видит остальной код: им
помечаются запросы (`requests.api_client`), по нему считается частота. Сам
ключ дальше этого модуля не уходит и не пишется ни в лог, ни в ответ.

Ограничение частоты держится в памяти процесса: на десятки запросов в месяц
отдельное хранилище — лишняя движущаяся часть (CLAUDE.md, «без Redis»).
Перезапуск процесса счётчики обнуляет — это осознанная цена.
"""

from __future__ import annotations

import logging
import math
import secrets
import time
from collections import deque
from collections.abc import Callable, Mapping
from functools import lru_cache

log = logging.getLogger(__name__)

#: Окна ограничения частоты, секунды.
RUNS_WINDOW_SECONDS = 3600.0
READS_WINDOW_SECONDS = 60.0


@lru_cache(maxsize=4)
def parse_api_keys(raw: str) -> tuple[tuple[str, bytes], ...]:
    """Разобрать `SCOUT_API_KEYS` в пары (имя приложения, ключ байтами).

    Пустая строка — пустой набор, API выключен. Испорченная запись (нет
    двоеточия, пустое имя или ключ) пропускается с предупреждением в лог —
    по номеру записи, без её содержимого: в записи лежит ключ. Результат
    кэшируется по строке окружения: разбор и предупреждение — один раз, а не
    на каждом вызове API.
    """
    pairs: list[tuple[str, bytes]] = []
    for index, chunk in enumerate(raw.split(","), start=1):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, separator, key = chunk.partition(":")
        name, key = name.strip(), key.strip()
        if not separator or not name or not key:
            log.warning("SCOUT_API_KEYS: запись №%s пропущена — ожидается вид имя:ключ", index)
            continue
        pairs.append((name, key.encode("utf-8")))
    return tuple(pairs)


def match_api_key(presented: str, keys: tuple[tuple[str, bytes], ...]) -> str | None:
    """Имя приложения, чей ключ совпал с предъявленным, иначе `None`.

    Сравнение `secrets.compare_digest` по байтам (на строках с не-ASCII оно
    падает `TypeError`) и по всем ключам без раннего выхода: время ответа не
    подсказывает, на каком ключе совпало.
    """
    presented_bytes = presented.encode("utf-8")
    matched: str | None = None
    for name, key in keys:
        if secrets.compare_digest(presented_bytes, key) and matched is None:
            matched = name
    return matched


def presented_key(headers: Mapping[str, str]) -> str | None:
    """Ключ из `Authorization: Bearer …` или `X-API-Key`; пусто — `None`."""
    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    api_key = headers.get("x-api-key", "").strip()
    return api_key or None


class SlidingWindowLimiter:
    """Скользящее окно событий на ключ: «не больше `limit` за `window` секунд».

    `check` и `record` разделены намеренно: запуск, отказанный по другой
    причине (система занята другим прогоном), попытку не расходует.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: dict[tuple[str, str], deque[float]] = {}

    def _window(self, key: tuple[str, str], window: float) -> deque[float]:
        events = self._events.setdefault(key, deque())
        threshold = self._clock() - window
        while events and events[0] <= threshold:
            events.popleft()
        return events

    def check(self, key: tuple[str, str], *, limit: int, window: float) -> int | None:
        """Сколько секунд ждать до следующей разрешённой попытки; `None` — можно сейчас."""
        events = self._window(key, window)
        if len(events) < limit:
            return None
        wait = events[0] + window - self._clock()
        return max(1, math.ceil(wait))

    def record(self, key: tuple[str, str]) -> None:
        """Засчитать попытку."""
        self._events.setdefault(key, deque()).append(self._clock())

    def hit(self, key: tuple[str, str], *, limit: int, window: float) -> int | None:
        """`check` и, если можно, сразу `record` — для чтений, где отказать иначе нечем."""
        retry_after = self.check(key, limit=limit, window=window)
        if retry_after is None:
            self.record(key)
        return retry_after

    def reset(self) -> None:
        """Обнулить все счётчики (тесты и перезапуск процесса)."""
        self._events.clear()


#: Общий на процесс ограничитель `/api/v1`.
limiter = SlidingWindowLimiter()
