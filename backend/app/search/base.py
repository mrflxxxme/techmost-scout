"""Контракт поискового адаптера.

Каждый источник (Crossref, КиберЛенинка, витрина партнёров, веб-поиск)
реализует один и тот же интерфейс. Пайплайн не знает, откуда пришёл кандидат —
он видит только список Candidate и имя провайдера.

Главное правило: адаптер никогда не роняет пайплайн. Если источник недоступен,
адаптер возвращает пустой список и поднимает флаг unavailable, а отчёт честно
пишет, какого источника в нём не хватает.
"""

from __future__ import annotations

import abc
import asyncio
import logging

import httpx

from app.schemas import Candidate, SearchQuery, SourceType

log = logging.getLogger(__name__)


class SearchAdapter(abc.ABC):
    """Базовый адаптер источника."""

    name: str = "base"
    source_types: tuple[SourceType, ...] = ()
    requires_key: bool = False
    #: Пауза между запросами, если у источника есть предел частоты
    min_interval_seconds: float = 0.0

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    @abc.abstractmethod
    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        """Найти кандидатов по одной формулировке."""

    async def health(self) -> tuple[bool, str]:
        """Проверка для `scout doctor`. По умолчанию — пробный поиск."""
        try:
            probe = SearchQuery(
                text="катализатор",
                source_type=self.source_types[0] if self.source_types else SourceType.PUBLICATION,
                lang="ru",
                rationale="health probe",
            )
            found = await self.search(probe, limit=1)
        except Exception as exc:  # noqa: BLE001 — доктор не должен падать
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"ок, кандидатов в пробе: {len(found)}"

    async def _throttle(self) -> None:
        """Уважаем предел частоты источника: arXiv просит 1 запрос в 3 секунды."""
        if self.min_interval_seconds <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self.min_interval_seconds - (loop.time() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = loop.time()

    async def _get_json(self, url: str, **kwargs) -> dict | list | None:
        """GET с троттлингом. None означает «источник не ответил»."""
        await self._throttle()
        try:
            response = await self._client.get(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("источник %s не ответил на %s: %s", self.name, url, exc)
            return None


class SearchError(RuntimeError):
    pass
