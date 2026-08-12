"""Semantic Scholar — публикации с аннотациями и ссылками на открытые PDF.

Бесплатно, ключ необязателен (requires_key = False). Разница такая:

* без ключа лимит общий на всех анонимных клиентов (порядка 1 запроса в
  секунду, и он периодически исчерпан кем-то другим) — тогда приходит HTTP 429;
* с ключом (api.semanticscholar.org/api-key, выдают по заявке) лимит личный.

Поэтому 429 здесь — ожидаемая ситуация, а не авария в смысле кода, но источник
в этот раз всё равно ничего не отдал: адаптер поднимает SearchError, отказ
ловит `_safe_search` оркестратора, и отчёт честно отметит, что источника не
хватило (инвариант №5), вместо того чтобы молча остаться с пустым списком.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, SearchError

log = logging.getLogger(__name__)

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = "title,abstract,year,authors,externalIds,url,openAccessPdf"
MAX_LIMIT = 100
MAX_SNIPPET = 700
MAX_AUTHORS = 12

_WS = re.compile(r"\s+")


class SemanticScholarAdapter(SearchAdapter):
    """Поиск публикаций по релевантности."""

    name = "semanticscholar"
    source_types = (SourceType.PUBLICATION,)
    #: Ключ не обязателен — адаптер включается всегда
    requires_key = False
    #: Анонимный пул примерно 1 rps; с ключом можно опустить
    min_interval_seconds = 1.0

    def __init__(self, client: httpx.AsyncClient, *, api_key: str | None = None) -> None:
        super().__init__(client)
        self._api_key = api_key or None
        if self._api_key:
            self.min_interval_seconds = 0.2

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        params = {
            "query": query.text,
            "limit": str(max(1, min(limit, MAX_LIMIT))),
            "fields": FIELDS,
        }
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["x-api-key"] = self._api_key

        payload = await self._request(params, headers)
        if payload is None:
            return []

        candidates: list[Candidate] = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            candidate = self._to_candidate(item)
            if candidate is not None:
                candidates.append(candidate)
        return candidates[:limit]

    async def _request(self, params: dict[str, str], headers: dict[str, str]) -> dict[str, Any] | None:
        """Свой GET вместо _get_json: нужно отдельно разобрать 429.

        Транспортный отказ (HTTP-ошибка, 429, нечитаемый ответ) поднимается
        как SearchError — _safe_search оркестратора поймает его и честно
        отметит источник недоступным (инвариант №5). 429 здесь ожидаемая, но
        всё равно недоступность: анонимный пул общий и периодически исчерпан
        другими клиентами."""
        await self._throttle()
        try:
            response = await self._client.get(API_URL, params=params, headers=headers)
            if response.status_code == 429:
                raise SearchError(
                    "semanticscholar: лимит запросов исчерпан (429)"
                    + ("" if self._api_key else ", ключ не задан и пул общий")
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(f"semanticscholar не ответил: {exc}") from exc
        return payload if isinstance(payload, dict) else None

    def _to_candidate(self, item: dict[str, Any]) -> Candidate | None:
        title = _clean(item.get("title"))
        if not title:
            return None

        external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        doi = _clean(external.get("DOI"))
        open_pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
        # Приоритет ссылки: DOI (стабильна) → страница S2 → открытый PDF
        url = f"https://doi.org/{doi}" if doi else _clean(item.get("url")) or _clean(open_pdf.get("url"))
        if not url:
            return None

        try:
            return Candidate(
                source_type=SourceType.PUBLICATION,
                title=title,
                url=url,
                authors=[
                    name
                    for author in (item.get("authors") or [])
                    if isinstance(author, dict) and (name := _clean(author.get("name")))
                ][:MAX_AUTHORS],
                year=_year(item.get("year")),
                snippet=_clean(item.get("abstract"))[:MAX_SNIPPET],
                raw_id=doi or _clean(external.get("CorpusId")) or None,
                pdf_url=_clean(open_pdf.get("url")) or None,
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("semanticscholar: запись отброшена (%s): %s", url, exc)
            return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return _WS.sub(" ", str(value)).strip()


def _year(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
