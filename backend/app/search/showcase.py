"""Витрина разработок партнёрских институтов — приоритетный слой поиска.

Это не внешний API, а наша собственная таблица (модель ShowcaseItem в
backend/app/models.py). Записи туда кладут руками сотрудники ЦТТ, поэтому по
ним у нас уже есть контакт разработчика и подтверждённая готовность — такие
кандидаты ценнее любой публикации и идут в отчёт первыми.

Два инженерных решения, которые видно не сразу:

* Модель и SQLAlchemy импортируем лениво, внутри метода. Иначе модуль search
  начал бы тянуть слой БД (циклический импорт: models → schemas → search).
  Если модели ещё нет (этап разработки не дошёл), пишем предупреждение в лог и
  возвращаем пустой список — пайплайн от этого не падает.
* Фильтруем в Python, а не в SQL. Витрина — это десятки-сотни записей, а вот
  регистронезависимый поиск по тексту и по tags (список или строка) на разных
  диалектах SQL пишется по-разному. Простота дороже микросекунд.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter

log = logging.getLogger(__name__)

#: Префикс, по которому в отчёте видно происхождение кандидата
TRUSTED_PREFIX = "[проверенный партнёр] "
MAX_SNIPPET = 700
MAX_AUTHORS = 12

_WORD = re.compile(r"[\w\-]+", re.UNICODE)
_WS = re.compile(r"\s+")
_STEM_LEN = 6
_STOPWORDS = frozenset(
    {
        "для",
        "при",
        "что",
        "как",
        "или",
        "это",
        "его",
        "уже",
        "без",
        "над",
        "под",
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "are",
        "was",
    }
)


class ShowcaseAdapter(SearchAdapter):
    """Поиск по витрине партнёров: title / description / tags."""

    name = "showcase"
    source_types = (SourceType.SHOWCASE,)
    requires_key = False

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(client)
        self._session_factory = session_factory

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        items = await self._load_items()
        if not items:
            return []

        keywords = _keywords(query.text)
        if not keywords:
            return []
        needed = max(1, (len(keywords) + 1) // 2)

        scored: list[tuple[int, Candidate]] = []
        for item in items:
            haystack = " ".join(
                (
                    _attr(item, "title"),
                    _attr(item, "description"),
                    _tags(item),
                )
            )
            matched = _match_score(keywords, haystack)
            if matched < needed:
                continue
            candidate = self._to_candidate(item)
            if candidate is not None:
                scored.append((matched, candidate))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [candidate for _, candidate in scored[:limit]]

    async def health(self) -> tuple[bool, str]:
        """Честный ответ для `scout doctor`: слоя может ещё не быть."""
        items = await self._load_items()
        if items is None:
            return False, "витрина недоступна: нет модели ShowcaseItem или сессии БД"
        return True, f"ок, записей в витрине: {len(items)}"

    # --- доступ к БД ----------------------------------------------------

    async def _load_items(self) -> list[Any] | None:
        """Все записи витрины. None означает «слой ещё не готов»."""
        try:
            from app.models import ShowcaseItem  # локальный импорт против цикла
        except (ImportError, AttributeError):
            log.warning("витрина: модель ShowcaseItem ещё не создана — слой пропущен")
            return None
        try:
            from sqlalchemy import select
        except ImportError:
            log.warning("витрина: sqlalchemy недоступна — слой пропущен")
            return None

        factory = self._session_factory or _discover_session_factory()
        if factory is None:
            log.warning("витрина: не найдена фабрика сессий БД — слой пропущен")
            return None

        try:
            statement = select(ShowcaseItem)
            context = factory()
            # Поддерживаем и async-, и обычную сессию: слой БД ещё не устоялся
            if hasattr(context, "__aenter__"):
                async with context as session:
                    result = await session.execute(statement)
                    return list(result.scalars().all())
            with context as session:
                return list(session.execute(statement).scalars().all())
        except Exception as exc:
            log.warning("витрина: запрос к БД не удался: %s", exc)
            return None

    # --- разбор ---------------------------------------------------------

    def _to_candidate(self, item: Any) -> Candidate | None:
        title = _attr(item, "title")
        url = _attr(item, "url")
        if not title or not url:
            log.debug("витрина: запись без названия или ссылки пропущена (id=%s)", _attr(item, "id"))
            return None

        # Главные преимущества витрины перед публикацией — заявленная готовность,
        # контакт и пометка о том, кто разработку подтвердил. Складываем их в
        # snippet: дальше по пайплайну это единственное текстовое поле кандидата.
        maturity = _attr(item, "maturity_stage")
        parts = (
            f"готовность: {maturity}." if maturity else "",
            _attr(item, "description"),
            f"контакт: {contact}" if (contact := _attr(item, "contact") or _attr(item, "contact_person")) else "",
            f"подтверждение: {note}" if (note := _attr(item, "source_note")) else "",
        )
        snippet = TRUSTED_PREFIX + " ".join(part for part in parts if part)

        try:
            return Candidate(
                source_type=SourceType.SHOWCASE,
                title=title,
                url=url,
                org=_attr(item, "org") or _attr(item, "organization") or None,
                authors=_authors(item),
                year=_year(item),
                snippet=snippet[:MAX_SNIPPET],
                raw_id=f"showcase:{_attr(item, 'id')}" if _attr(item, "id") else None,
                provider=self.name,
            )
        except ValidationError as exc:
            log.warning("витрина: запись отброшена (%s): %s", url, exc)
            return None


def _discover_session_factory() -> Callable[[], Any] | None:
    """Найти фабрику сессий, если её не передали в конструктор."""
    try:
        from app import db  # type: ignore[attr-defined]
    except ImportError:
        return None
    # SessionFactory — каноническое имя в app/db.py; остальные оставлены на
    # случай переименования слоя БД, чтобы витрина не отваливалась молча.
    known_names = ("SessionFactory", "async_session_factory", "session_factory", "AsyncSessionLocal", "SessionLocal")
    for attribute in known_names:
        factory = getattr(db, attribute, None)
        if factory is not None:
            return factory
    return None


def _attr(item: Any, name: str) -> str:
    value = getattr(item, name, None)
    if value is None:
        return ""
    return _WS.sub(" ", str(value)).strip()


def _tags(item: Any) -> str:
    """tags в модели могут быть списком (JSON-поле) или строкой через запятую."""
    value = getattr(item, "tags", None)
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(tag) for tag in value)
    return str(value)


def _authors(item: Any) -> list[str]:
    value = getattr(item, "authors", None) or getattr(item, "team", None)
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(author).strip() for author in value if str(author).strip()][:MAX_AUTHORS]
    return [part.strip() for part in str(value).split(",") if part.strip()][:MAX_AUTHORS]


def _year(item: Any) -> int | None:
    for name in ("year", "created_year"):
        value = getattr(item, name, None)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    created = getattr(item, "created_at", None)
    return getattr(created, "year", None)


def _keywords(text: str) -> list[str]:
    words = [word.lower() for word in _WORD.findall(text)]
    return [word for word in words if len(word) >= 3 and word not in _STOPWORDS]


def _match_score(keywords: list[str], haystack: str) -> int:
    lowered = haystack.lower()
    return sum(1 for word in keywords if word[:_STEM_LEN] in lowered)
