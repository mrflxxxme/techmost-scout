"""Crossref — основной бесплатный источник по публикациям.

Почему он рабочая лошадка: полный охват DOI (научные журналы, сборники,
препринты у части издателей), никакого ключа, лимит — до 50 запросов/сек.

Условие вежливого доступа (polite pool): в каждом запросе присылать User-Agent
с контактным mailto. За это Crossref пускает в быстрый пул и не режет по
лимиту; без mailto запросы уходят в общую очередь и начинают ловить 429.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter

log = logging.getLogger(__name__)

API_URL = "https://api.crossref.org/works"
CONTACT_EMAIL = "uklonskiy.k@gmail.com"
#: Условие polite pool — Crossref смотрит именно на User-Agent с mailto
USER_AGENT = f"TechMostScout/1.0 (mailto:{CONTACT_EMAIL})"
#: Отсекаем совсем древние работы: для промышленного внедрения они бесполезны
FRESHNESS_YEARS = 12
#: select экономит трафик — без него Crossref отдаёт мегабайты метаданных
SELECT_FIELDS = "DOI,title,container-title,author,issued,abstract,URL"
MAX_ROWS = 100
MAX_SNIPPET = 700
MAX_AUTHORS = 12

#: В abstract Crossref часто лежит JATS-разметка (<jats:p>, <jats:sec>…)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


class CrossrefAdapter(SearchAdapter):
    """Поиск публикаций по библиографическому запросу."""

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)
    requires_key = False
    #: Формально до 50 rps, но пайплайн шлёт запросы пачками — держим паузу
    min_interval_seconds = 0.1

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        params = {
            "query.bibliographic": query.text,
            "rows": str(max(1, min(limit, MAX_ROWS))),
            "filter": f"from-pub-date:{self._from_pub_date()}",
            "select": SELECT_FIELDS,
            # mailto дублируем параметром: часть прокси срезает User-Agent
            "mailto": CONTACT_EMAIL,
        }
        payload = await self._get_json(
            API_URL,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if not isinstance(payload, dict):
            return []

        items = payload.get("message", {}).get("items")
        if not isinstance(items, list):
            log.warning("crossref: ответ без message.items")
            return []

        candidates: list[Candidate] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate = self._to_candidate(item)
            if candidate is not None:
                candidates.append(candidate)
        return candidates[:limit]

    @staticmethod
    def _from_pub_date() -> str:
        return f"{date.today().year - FRESHNESS_YEARS}-01-01"

    def _to_candidate(self, item: dict[str, Any]) -> Candidate | None:
        title = _first_str(item.get("title"))
        if not title:
            return None

        doi = _clean_str(item.get("DOI"))
        # Без ссылки кандидат бесполезен: карточку нечем подтвердить
        url = f"https://doi.org/{doi}" if doi else _clean_str(item.get("URL"))
        if not url:
            return None

        journal = _first_str(item.get("container-title"))
        abstract = _strip_markup(item.get("abstract"))
        snippet = " — ".join(part for part in (journal, abstract) if part)[:MAX_SNIPPET]

        try:
            return Candidate(
                source_type=SourceType.PUBLICATION,
                title=title,
                url=url,
                org=_first_affiliation(item.get("author")) or journal or None,
                authors=_authors(item.get("author")),
                year=_issued_year(item.get("issued")),
                snippet=snippet,
                raw_id=doi or None,
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("crossref: запись отброшена (%s): %s", url, exc)
            return None


def _clean_str(value: Any) -> str:
    return _WS.sub(" ", str(value)).strip() if isinstance(value, str) else ""


def _first_str(value: Any) -> str:
    """title и container-title в Crossref — массивы, берём первый элемент."""
    if isinstance(value, list):
        for element in value:
            cleaned = _clean_str(element)
            if cleaned:
                return cleaned
        return ""
    return _clean_str(value)


def _strip_markup(value: Any) -> str:
    """Вычистить JATS-разметку и HTML-сущности из аннотации."""
    raw = value if isinstance(value, str) else ""
    if not raw:
        return ""
    text = _TAG.sub(" ", raw)
    return _WS.sub(" ", html.unescape(text)).strip()


def _authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for author in value[:MAX_AUTHORS]:
        if not isinstance(author, dict):
            continue
        family = _clean_str(author.get("family"))
        given = _clean_str(author.get("given"))
        full = " ".join(part for part in (family, given) if part) or _clean_str(author.get("name"))
        if full:
            names.append(full)
    return names


def _first_affiliation(value: Any) -> str:
    """Аффилиация первого автора — самая полезная зацепка на разработчика."""
    if not isinstance(value, list):
        return ""
    for author in value:
        if not isinstance(author, dict):
            continue
        for affiliation in author.get("affiliation") or []:
            if isinstance(affiliation, dict):
                name = _clean_str(affiliation.get("name"))
                if name:
                    return name
    return ""


def _issued_year(value: Any) -> int | None:
    """issued.date-parts выглядит как [[2021, 5, 3]] — нужен только год."""
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts:
        return None
    first = parts[0]
    if not isinstance(first, list) or not first:
        return None
    try:
        return int(first[0])
    except (TypeError, ValueError):
        return None
