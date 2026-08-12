"""arXiv — препринты по физике, химии, материаловедению, инженерии.

Бесплатно, без ключа. Жёсткий лимит: не чаще 1 запроса в 3 секунды (условие
публичного API, при нарушении банят по IP) — отсюда min_interval_seconds = 3.0.
Ответ — Atom XML, не JSON, поэтому базовый _get_json здесь не годится.

Оговорка по языку: индекс полностью англоязычный. Русскую формулировку arXiv
не поймёт, поэтому по lang="ru" мы честно ничего не ищем — пусть планировщик
запросов присылает англоязычную формулировку.
"""

from __future__ import annotations

import logging
import re

import httpx
from defusedxml import ElementTree as ET  # noqa: N817 — ET общепринятое сокращение; defusedxml защищает от XML-атак
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, SearchError

log = logging.getLogger(__name__)

API_URL = "http://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom"}
MAX_RESULTS = 100
MAX_SNIPPET = 700
MAX_AUTHORS = 12
#: Больше термов запрос не выдержит: arXiv режет длинные булевы выражения
MAX_TERMS = 6
#: Фразовый поиск точнее, но работает только на коротких формулировках
PHRASE_WORD_LIMIT = 6

_WORD = re.compile(r"[A-Za-z0-9\-]+")
_WS = re.compile(r"\s+")
_ARXIV_ID = re.compile(r"arxiv\.org/abs/(.+)$", re.IGNORECASE)
_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "from", "that", "this", "are", "was", "of", "in", "on", "to", "a", "an"}
)


class ArxivAdapter(SearchAdapter):
    """Поиск препринтов по названию и аннотации."""

    name = "arxiv"
    source_types = (SourceType.PUBLICATION,)
    requires_key = False
    #: Условие публичного API arXiv: 1 запрос в 3 секунды
    min_interval_seconds = 3.0

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        if query.lang == "ru":
            log.debug("arxiv: пропускаем русскую формулировку «%s» — индекс англоязычный", query.text)
            return []

        search_query = _build_search_query(query.text)
        if not search_query:
            return []

        params = {
            "search_query": search_query,
            "start": "0",
            "max_results": str(max(1, min(limit, MAX_RESULTS))),
        }
        body = await self._fetch(params)
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            log.warning("arxiv: Atom не разобран: %s", exc)
            return []

        candidates: list[Candidate] = []
        for entry in root.iterfind("atom:entry", NS):
            candidate = self._to_candidate(entry)
            if candidate is not None:
                candidates.append(candidate)
        return candidates[:limit]

    async def _fetch(self, params: dict[str, str]) -> str:
        """GET к arXiv. Транспортный отказ поднимается как SearchError —
        _safe_search оркестратора поймает его и честно отметит источник
        недоступным (инвариант №5), вместо того чтобы тихо вернуть []."""
        await self._throttle()
        try:
            response = await self._client.get(API_URL, params=params)
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            raise SearchError(f"arxiv не ответил на «{params.get('search_query')}»: {exc}") from exc

    def _to_candidate(self, entry: ET.Element) -> Candidate | None:
        title = _text(entry, "atom:title")
        # Ошибку arXiv отдаёт обычной записью с title="Error"
        if not title or title.lower().startswith("error"):
            return None

        url = _text(entry, "atom:id")
        if not url:
            return None
        # http:// в id — исторический артефакт, https работает и надёжнее
        url = url.replace("http://arxiv.org/", "https://arxiv.org/", 1)

        summary = _text(entry, "atom:summary")
        authors = [name for author in entry.iterfind("atom:author", NS) if (name := _text(author, "atom:name"))][
            :MAX_AUTHORS
        ]

        try:
            return Candidate(
                source_type=SourceType.PUBLICATION,
                title=title,
                url=url,
                authors=authors,
                year=_year(_text(entry, "atom:published")),
                snippet=summary[:MAX_SNIPPET],
                raw_id=_raw_id(url),
                pdf_url=_pdf_url(url),
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("arxiv: запись отброшена (%s): %s", url, exc)
            return None


def _build_search_query(text: str) -> str:
    """Собрать выражение для search_query.

    Короткую формулировку ищем фразой по названию и аннотации — так меньше
    мусора. Длинную разбираем на слова и требуем все через all:, иначе arXiv
    вернёт всё подряд.
    """
    words = [word.lower() for word in _WORD.findall(text)]
    words = [word for word in words if len(word) >= 3 and word not in _STOPWORDS]
    if not words:
        return ""
    if len(words) <= PHRASE_WORD_LIMIT:
        phrase = " ".join(words)
        return f'ti:"{phrase}" OR abs:"{phrase}"'
    return " AND ".join(f"all:{word}" for word in words[:MAX_TERMS])


def _text(node: ET.Element, path: str) -> str:
    found = node.find(path, NS)
    return _WS.sub(" ", found.text or "").strip() if found is not None else ""


def _year(published: str) -> int | None:
    match = re.match(r"(19|20)\d{2}", published)
    return int(match.group(0)) if match else None


def _raw_id(url: str) -> str | None:
    """Из https://arxiv.org/abs/2401.01234v2 делаем arXiv:2401.01234v2."""
    match = _ARXIV_ID.search(url)
    return f"arXiv:{match.group(1)}" if match else None


def _pdf_url(url: str) -> str | None:
    """Адрес PDF выводится из id записи (…/abs/<id> → …/pdf/<id>), без
    дополнительных запросов: arXiv отдаёт PDF по предсказуемому пути."""
    match = _ARXIV_ID.search(url)
    return f"https://arxiv.org/pdf/{match.group(1)}" if match else None
