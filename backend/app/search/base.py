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
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.schemas import Candidate, SearchQuery, SourceType

log = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    """Свести адрес к виду, одинаковому для дублей.

    Ключ склейки веб-страниц: отбрасываем схему, www, параметры запроса и
    хвостовой слэш. Одна и та же страница приходит из разных поисковиков в
    разном написании, а склеивать веб по названию нельзя — у страниц заголовки
    совпадают сплошь и рядом («Технологии», «Разработки»), и половина находок
    исчезла бы как мнимые дубли.

    Живёт здесь, а не в конкретном адаптере, потому что нужна всем источникам
    типа WEB: только общий ключ склеивает страницу из Яндекса со страницей из
    любого другого веб-источника.
    """
    try:
        parts = urlsplit(url.strip().lower())
    except ValueError:
        return url.strip().lower()
    host = parts.netloc.removeprefix("www.")
    path = parts.path.rstrip("/")
    return urlunsplit(("", host, path, "", "")).lstrip("/")


class SearchAdapter(abc.ABC):
    """Базовый адаптер источника."""

    name: str = "base"
    source_types: tuple[SourceType, ...] = ()
    requires_key: bool = False
    #: Пауза между запросами, если у источника есть предел частоты
    min_interval_seconds: float = 0.0
    #: Нужна ли выдаче источника нормализация моделью — стадия extractor (Э2-5).
    #: Источник со структурированным API разбирается кодом и флага не поднимает:
    #: гонять его через модель значит жечь токены без выигрыша.
    needs_extraction: bool = False

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    @abc.abstractmethod
    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        """Найти кандидатов по одной формулировке."""

    async def search_raw(self, query: SearchQuery, *, limit: int = 20) -> tuple[list[Candidate], list[dict]]:
        """Кандидаты и сырые записи ТОГО ЖЕ ответа источника.

        Второй элемент пары — вход стадии extractor (Э2-5). Отдельный метод, а
        не расширение `search`: публичный контракт `search` менять нельзя, а
        второй заход в источник за той же выдачей недопустим — у платного
        источника он попросту удваивает счёт (Р-8 плана). Поэтому адаптер с
        `needs_extraction = True` обязан отдать записи из уже полученного
        ответа, а не сходить за ними ещё раз.

        По умолчанию сырых записей нет: источник со структурированным API
        разбирается кодом, и нормализовать моделью у него нечего.
        """
        return await self.search(query, limit=limit), []

    def contour_label(self, query: SearchQuery) -> str:
        """Метка контура выдачи для формулировки — ключ `hits_by_provider` в
        оркестраторе.

        По умолчанию совпадает с именем адаптера: один источник — один контур.
        Адаптер, что выбирает контур по самой формулировке (см.
        `YandexSearchAdapter`), переопределяет метод — правило выбора и его
        метка живут в одном месте, а оркестратор просто складывает по метке.
        """
        return self.name

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
        except Exception as exc:
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
