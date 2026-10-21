"""Wikidata — бесплатный источник зарубежных аналогов: компании и продукты.

Работает по типу `analog` (Э6-2б): когда ищется не научная работа, а готовый
рыночный аналог, Wikidata отвечает на вопрос «какая компания или продукт
называется так» и отдаёт то, что редактор внёс в карточку: страну, год
основания, сайт, отрасль. Ключа нет; от нас требуется только честный
`User-Agent` с контактом (политика Wikimedia).

Как устроен запрос. Поиск идёт сервисом `wikibase:mwapi` / `EntitySearch` —
он ищет по названиям и псевдонимам на языке формулировки. Выдача поиска
ограничена подзапросом, чтобы сам SPARQL не разрастался, и фильтруется до
компаний и продуктов по `P31` (экземпляр класса). Подклассы через `P279*`
не раскрываются сознательно: на живой проверке такой запрос уходил в таймаут
сервиса (504), а прямой `P31` по перечню классов отвечает за секунды.

Страна. `country` заполняется из `P17 → P297` (код ISO 3166-1 alpha-2) только
когда значение ровно одно: компания с двумя странами — не повод угадывать, и
отчёт честно оставляет страну пустой.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas import COUNTRY_CODE_PATTERN, Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, SearchError
from app.search.crossref import CONTACT_EMAIL
from app.search.query_shortening import english_content_words, shorten_ru_query

log = logging.getLogger(__name__)

SPARQL_URL = "https://query.wikidata.org/sparql"
ENTITY_URL = "https://www.wikidata.org/wiki/"
#: Сколько сущностей берём из поиска до отбора по типу: часть найденного —
#: люди, места, понятия, и отсеется. Предел самого сервиса — 50
SEARCH_ENTITIES = 50
#: Потолок строк ответа: на сущность приходится по строке на каждое сочетание
#: страны, сайта и отрасли
MAX_ROWS = 400
#: Длина формулировки, выше которой поиск по названиям заведомо пуст
MAX_QUERY_CHARS = 120
#: Слов в укороченной формулировке — для единственного повтора при пустой выдаче
SHORT_QUERY_WORDS = 3
MAX_SNIPPET = 700
#: Классы Wikidata, которые считаем компанией или продуктом: business,
#: company, enterprise, public company, software company, commercial
#: organization; product, brand, software
ANALOG_CLASSES = (
    "Q4830453",
    "Q783794",
    "Q6881511",
    "Q891723",
    "Q1058914",
    "Q21980538",
    "Q2424752",
    "Q431289",
    "Q7397",
)

_QUERY = """SELECT ?item ?itemLabel ?itemDescription ?inception ?site ?cc ?industryLabel WHERE {{
  {{
    SELECT ?item WHERE {{
      SERVICE wikibase:mwapi {{
        bd:serviceParam wikibase:api "EntitySearch" .
        bd:serviceParam wikibase:endpoint "www.wikidata.org" .
        bd:serviceParam mwapi:search "{text}" .
        bd:serviceParam mwapi:language "{lang}" .
        ?item wikibase:apiOutputItem mwapi:item .
      }}
    }} LIMIT {entities}
  }}
  ?item wdt:P31 ?type .
  VALUES ?type {{ {classes} }}
  OPTIONAL {{ ?item wdt:P571 ?inception }}
  OPTIONAL {{ ?item wdt:P856 ?site }}
  OPTIONAL {{ ?item wdt:P17 ?country . ?country wdt:P297 ?cc }}
  OPTIONAL {{ ?item wdt:P452 ?industry }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{labels}" . }}
}} LIMIT {rows}"""

_ENTITY_ID = re.compile(r"/entity/(Q\d+)$")
_BARE_ID = re.compile(r"Q\d+")
_YEAR = re.compile(r"^(\d{4})-")
_WS = re.compile(r"\s+")
_COUNTRY = re.compile(COUNTRY_CODE_PATTERN)


class WikidataAdapter(SearchAdapter):
    """Компании и продукты из Wikidata как зарубежные аналоги."""

    name = "wikidata"
    source_types = (SourceType.ANALOG,)
    requires_key = False
    needs_extraction = False
    #: Wikimedia просит не стучаться чаще раза в секунду с одного адреса
    min_interval_seconds = 1.0

    def __init__(self, client: httpx.AsyncClient, *, contact_email: str | None = None) -> None:
        super().__init__(client)
        self._contact_email = contact_email or CONTACT_EMAIL

    @property
    def user_agent(self) -> str:
        """Честное представление источнику: кто стучится и куда писать."""
        return f"TechMostScout/1.0 (mailto:{self._contact_email})"

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        """Кандидаты по одной формулировке. Отказ источника — `SearchError`.

        Поиск по названиям душит длинную формулировку до пустой выдачи, поэтому
        при пустом ответе делается ровно один повтор с первыми словами
        формулировки, детерминированно и без модели.
        """
        text = _clean_text(query.text)
        if not text:
            return []
        candidates = await self._search_once(text, query.lang, limit)
        short = _shorten(text, query.lang)
        if not candidates and short and short.lower() != text.lower():
            candidates = await self._search_once(short, query.lang, limit)
        return candidates[:limit]

    async def _search_once(self, text: str, lang: str, limit: int) -> list[Candidate]:
        """Один SPARQL-запрос и разбор строк ответа в кандидатов."""
        sparql = _QUERY.format(
            text=_escape(text),
            lang="ru" if lang == "ru" else "en",
            labels="ru,en" if lang == "ru" else "en,ru",
            entities=SEARCH_ENTITIES,
            classes=" ".join(f"wd:{cls}" for cls in ANALOG_CLASSES),
            rows=MAX_ROWS,
        )
        payload = await self._fetch(sparql, text)
        bindings = _bindings(payload)
        items: dict[str, list[dict[str, Any]]] = {}
        for row in bindings:
            if not isinstance(row, dict):
                continue
            match = _ENTITY_ID.search(_value(row, "item"))
            if match:
                items.setdefault(match.group(1), []).append(row)
        candidates: list[Candidate] = []
        for entity_id, rows in items.items():
            candidate = self._to_candidate(entity_id, rows)
            if candidate is not None:
                candidates.append(candidate)
        log.info("wikidata: «%s» → %s компаний и продуктов", text, len(candidates))
        return candidates[: max(1, limit)]

    async def _fetch(self, sparql: str, text: str) -> Any:
        """GET к SPARQL-сервису. Транспортный отказ — `SearchError` (инвариант №5):
        его ловит `_safe_search`, и источник попадает в `sources_unavailable`."""
        await self._throttle()
        try:
            response = await self._client.get(
                SPARQL_URL,
                params={"query": sparql, "format": "json"},
                headers={"User-Agent": self.user_agent, "Accept": "application/sparql-results+json"},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            # Адрес в сообщение не кладём: в нём весь текст SPARQL-запроса
            raise SearchError(f"wikidata ответил HTTP {exc.response.status_code} на «{text}»") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(f"wikidata не ответил на «{text}»: {type(exc).__name__}") from exc

    def _to_candidate(self, entity_id: str, rows: list[dict[str, Any]]) -> Candidate | None:
        """Свести строки одной сущности в кандидата. Без названия — отбрасываем."""
        first = rows[0]
        title = _clean_text(_value(first, "itemLabel"))
        # Сущность без названия сервис подписывает её же номером — читать нечего
        if not title or title == entity_id:
            return None
        description = _clean_text(_value(first, "itemDescription"))
        countries = _distinct(rows, "cc")
        sites = _distinct(rows, "site")
        industries = [name for name in _distinct(rows, "industryLabel") if not _BARE_ID.fullmatch(name)]
        inception = next((year for year in (_year(_value(row, "inception")) for row in rows) if year), None)

        parts = [description]
        if countries:
            parts.append("страна: " + ", ".join(countries))
        if inception:
            parts.append(f"основана: {inception}")
        if sites:
            parts.append("сайт: " + sites[0])
        if industries:
            parts.append("отрасль: " + ", ".join(industries))
        country = countries[0] if len(countries) == 1 and _COUNTRY.fullmatch(countries[0]) else None
        try:
            return Candidate(
                source_type=SourceType.ANALOG,
                title=title,
                url=f"{ENTITY_URL}{entity_id}",
                year=inception,
                snippet="; ".join(part for part in parts if part)[:MAX_SNIPPET],
                raw_id=entity_id,
                provider=self.name,
                country=country,
            )
        except ValidationError as exc:
            log.debug("wikidata: запись %s отброшена: %s", entity_id, exc)
            return None


def _bindings(payload: Any) -> list[Any]:
    """`results.bindings` ответа. Иная форма — нечитаемый ответ, то есть отказ."""
    results = payload.get("results") if isinstance(payload, dict) else None
    bindings = results.get("bindings") if isinstance(results, dict) else None
    if not isinstance(bindings, list):
        raise SearchError("wikidata: в ответе нет results.bindings")
    return bindings


def _value(row: dict[str, Any], key: str) -> str:
    cell = row.get(key)
    value = cell.get("value") if isinstance(cell, dict) else None
    return value if isinstance(value, str) else ""


def _distinct(rows: list[dict[str, Any]], key: str) -> list[str]:
    """Различные значения поля по строкам сущности, в порядке появления."""
    seen: list[str] = []
    for row in rows:
        value = _clean_text(_value(row, key))
        if value and value not in seen:
            seen.append(value)
    return seen


def _year(value: str) -> int | None:
    """Год из `2006-01-01T00:00:00Z`. Годы до нашей эры (`-0500-…`) не берём."""
    match = _YEAR.match(value)
    return int(match.group(1)) if match else None


def _clean_text(value: str) -> str:
    return _WS.sub(" ", value).strip()


def _escape(text: str) -> str:
    """Экранировать формулировку для строкового литерала SPARQL: формулировку
    пишет модель по тексту заказчика, ей нельзя дать закрыть кавычку."""
    return text[:MAX_QUERY_CHARS].replace("\\", " ").replace('"', " ")


def _shorten(text: str, lang: str) -> str:
    if lang == "ru":
        return shorten_ru_query(text, word_limit=SHORT_QUERY_WORDS)
    return " ".join(english_content_words(text)[:SHORT_QUERY_WORDS])
