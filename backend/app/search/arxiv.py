"""arXiv — препринты по физике, химии, материаловедению, инженерии.

Бесплатно, без ключа. Жёсткий лимит: не чаще 1 запроса в 3 секунды (условие
публичного API, при нарушении банят по IP) — отсюда min_interval_seconds = 3.0.
Ответ — Atom XML, не JSON, поэтому базовый _get_json здесь не годится.

Оговорка по языку: индекс полностью англоязычный. Русскую формулировку arXiv
не поймёт, поэтому по lang="ru" мы честно ничего не ищем — пусть планировщик
запросов присылает англоязычную формулировку.

СЕМИСЛОВНЫЕ ФРАЗЫ ДАЮТ НОЛЬ (диагноз Э2-Ж, `docs/sources.md`). Формулировка
модели длиннее `PHRASE_WORD_LIMIT` (6 слов) уходит не фразовым поиском, а
булевым `AND` всех термов через `all:` — семь и более обязательных слов
почти никогда не встречаются в одном препринте одновременно, отсюда
`found=0`. Починка — тот же приём, что у КиберЛенинки (З-2) и Роспатента
(Д-2): при пустой выдаче на исходной формулировке ОДИН повтор с первыми 3–4
содержательными словами, собранными уже фразовым запросом (`SHORT_QUERY_WORDS
= 4` ≤ `PHRASE_WORD_LIMIT`, так что укороченная форма всегда идёт как фраза,
даже если исходная шла как AND). Больше одного повтора не делаем: темп 1
запрос в 3 секунды и уважение к лимиту публичного API важнее полноты подбора
формы запроса.
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

#: Только https: прокси среды режет незашифрованный http (403 на всех прогонах)
API_URL = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom"}
MAX_RESULTS = 100
MAX_SNIPPET = 700
MAX_AUTHORS = 12
#: Больше термов запрос не выдержит: arXiv режет длинные булевы выражения
MAX_TERMS = 6
#: Фразовый поиск точнее, но работает только на коротких формулировках
PHRASE_WORD_LIMIT = 6
#: Сколько содержательных слов оставлять при повторе на пустой выдаче — тот
#: же потолок, что у КиберЛенинки (З-2) и Роспатента (Д-2); меньше
#: `PHRASE_WORD_LIMIT`, поэтому короткая форма всегда идёт фразовым поиском.
SHORT_QUERY_WORDS = 4

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
        """Кандидаты по одной формулировке. Отказ источника — `SearchError`.

        Диагноз Э2-Ж: формулировка длиннее `PHRASE_WORD_LIMIT` уходит булевым
        `AND` всех слов и часто даёт `found=0`. При нулевой выдаче на исходной
        формулировке — один повтор с укороченной формой (первые 3–4
        содержательных слова, фразовым поиском, без модели). Больше одного
        повтора не делаем.
        """
        if query.lang == "ru":
            log.debug("arxiv: пропускаем русскую формулировку «%s» — индекс англоязычный", query.text)
            return []

        words = _content_words(query.text)
        if not words:
            return []
        search_query = _query_from_words(words)

        candidates = await self._search_once(search_query, limit=limit)

        short_words = words[:SHORT_QUERY_WORDS]
        short_query = _query_from_words(short_words)
        if not candidates and short_query != search_query:
            short_candidates = await self._search_once(short_query, limit=limit)
            log.info(
                "arxiv: «%s» → found=%s; повтор укороченной «%s» → found=%s",
                search_query,
                len(candidates),
                short_query,
                len(short_candidates),
            )
            return short_candidates[:limit]

        log.info("arxiv: «%s» → found=%s", search_query, len(candidates))
        return candidates[:limit]

    async def _search_once(self, search_query: str, *, limit: int) -> list[Candidate]:
        """Один запрос к arXiv, разбор Atom-ленты в кандидатов."""
        params = {
            "search_query": search_query,
            "start": "0",
            "max_results": str(max(1, min(limit, MAX_RESULTS))),
        }
        body = await self._fetch(params)
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            # Нечитаемое тело при коде 200 (прокси, WAF, обрезанный ответ) —
            # тот же транспортный отказ, что и HTTP-ошибка: инвариант №5
            # требует, чтобы он дошёл до sources_unavailable, а не тихо
            # превратился в «ничего не нашли».
            raise SearchError(f"arxiv: тело ответа не разобрано как XML: {exc}") from exc

        candidates: list[Candidate] = []
        for entry in root.iterfind("atom:entry", NS):
            candidate = self._to_candidate(entry)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

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
        raw_id, pdf_url = _arxiv_ids(url)

        try:
            return Candidate(
                source_type=SourceType.PUBLICATION,
                title=title,
                url=url,
                authors=authors,
                year=_year(_text(entry, "atom:published")),
                snippet=summary[:MAX_SNIPPET],
                raw_id=raw_id,
                pdf_url=pdf_url,
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("arxiv: запись отброшена (%s): %s", url, exc)
            return None


def _content_words(text: str) -> list[str]:
    """Содержательные слова формулировки: нижний регистр, короткие и
    стоп-слова отсеяны, порядок исходного текста сохранён."""
    words = [word.lower() for word in _WORD.findall(text)]
    return [word for word in words if len(word) >= 3 and word not in _STOPWORDS]


def _query_from_words(words: list[str]) -> str:
    """Собрать выражение для search_query из уже отфильтрованных слов.

    Короткий список слов ищем фразой по названию и аннотации — так меньше
    мусора. Длинный разбираем на слова и требуем все через all:, иначе arXiv
    вернёт всё подряд.
    """
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


def _arxiv_ids(url: str) -> tuple[str | None, str | None]:
    """Ключ склейки дублей и адрес PDF — оба выводятся из одного совпадения в
    URL записи (https://arxiv.org/abs/2401.01234v2 → arXiv:2401.01234v2 и
    https://arxiv.org/pdf/2401.01234v2), без дополнительных запросов."""
    match = _ARXIV_ID.search(url)
    if not match:
        return None, None
    ident = match.group(1)
    return f"arXiv:{ident}", f"https://arxiv.org/pdf/{ident}"
