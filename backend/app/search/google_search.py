"""Google — мировой охват открытого веба.

ЗАЧЕМ ЭТОТ ИСТОЧНИК
    Научные базы (Crossref, arXiv, Semantic Scholar) знают только про статьи, а
    решение чаще всего живёт не в статье: страница лаборатории, сайт спин-оффа,
    отчёт по проекту, презентация с конференции, каталог технологий института.
    Яндекс закрывает это по РФ, Google — по остальному миру: университеты США и
    Европы, азиатские институты, англоязычные технологические витрины.

    Разделение труда с `yandex_search`: Яндекс лучше индексирует русскоязычные
    сайты вузов, Google — всё остальное. Оба отдают SourceType.WEB и склеиваются
    по нормализованному URL, поэтому одна и та же страница из двух поисковиков в
    отчёт дважды не попадёт.

ИСТОЧНИК И ЦЕНА
    Google Programmable Search Engine, Custom Search JSON API:
        https://developers.google.com/custom-search/v1/overview
        GET https://www.googleapis.com/customsearch/v1
    Бесплатно 100 запросов в сутки, дальше 5 долларов за 1000, потолок 10 000 в
    сутки. Каждый вызов стоит денег, поэтому счётчик `queries_used` ведётся
    честно и попадает в стоимость прогона.

    Почему именно этот API, а не разбор страницы выдачи google.com: разбор
    выдачи запрещён условиями Google, ломается при каждой правке вёрстки и
    упирается в капчу с серверного адреса. Для продукта, который показывают
    промышленному заказчику, это неприемлемо — берём официальный интерфейс.

ЧТО ЗНАЧИТ «ВЕСЬ МИР» И ГДЕ ГРАНИЦА
    Поисковая система в консоли Google обязана быть настроена на режим «искать
    по всему вебу» (Search the entire web). Иначе CSE ищет только по списку
    сайтов, заданному вручную, и мировой охват превращается в десяток закладок.
    Как включить — в `docs/deps-manual.md`, пункт про Google.

    Честные пределы, о которых нельзя молчать в отчёте:
    * одна выдача — максимум 10 ссылок, до 100 по одной формулировке через
      постраничный обход; каждая страница — отдельный платный запрос;
    * это индекс Google, а не «все сайты мира»: закрытые разделы, содержимое за
      формой входа и то, что запрещено в robots.txt, туда не попадает.
    Поэтому охват растёт не постраничным обходом, а числом формулировок в плане
    поиска — по-разному сформулированный запрос достаёт разные пласты выдачи.

ЯЗЫК НАМЕРЕННО НЕ ОГРАНИЧИВАЕТСЯ
    Параметр `lr` (language restrict) по умолчанию не передаётся: задача
    источника — дотянуться куда угодно, а не остаться в одном языке. Русский
    запрос и так вернёт преимущественно русские страницы, английский —
    английские. Ограничение доступно через `lang_restrict`, если однажды
    понадобится сузить.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/customsearch/v1"

#: Жёсткие пределы самого API, а не наши предпочтения
MAX_PER_PAGE = 10
MAX_TOTAL = 100

#: Цена сверх бесплатной сотни в сутки: 5 долларов за 1000 запросов
USD_PER_QUERY = 0.005

MAX_SNIPPET = 700

_WS = re.compile(r"\s+")
_YEAR = re.compile(r"(19|20)\d{2}")

#: Где у страницы может лежать дата публикации. Порядок = доверие к метке.
_DATE_KEYS = (
    "article:published_time",
    "citation_publication_date",
    "og:updated_time",
    "date",
    "dc.date",
)


class GoogleSearchAdapter(SearchAdapter):
    """Поиск по мировому вебу через Programmable Search Engine."""

    name = "google_search"
    source_types = (SourceType.WEB,)
    #: Без ключа и идентификатора поисковой системы адаптер не включается
    requires_key = True
    #: Квота суточная, а не секундная; небольшая пауза — вежливость к API
    min_interval_seconds = 0.2

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str | None = None,
        cse_id: str | None = None,
        fx_rate: float = 100.0,
        lang_restrict: str | None = None,
    ) -> None:
        super().__init__(client)
        self._api_key = api_key or None
        self._cse_id = cse_id or None
        self._fx_rate = fx_rate
        self._lang_restrict = lang_restrict or None
        #: Сколько платных запросов израсходовано за жизнь адаптера
        self.queries_used = 0

    @property
    def estimated_cost_rub(self) -> float:
        """Во сколько обошёлся источник. Верхняя оценка: бесплатная сотня в
        сутки здесь не учитывается — пусть цифра пугает, а не убаюкивает."""
        return self.queries_used * USD_PER_QUERY * self._fx_rate

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        if not (self._api_key and self._cse_id):
            log.warning("google_search: нет ключа или идентификатора поисковой системы")
            return []

        wanted = max(1, min(limit, MAX_TOTAL))
        candidates: list[Candidate] = []
        seen: set[str] = set()

        # Постраничный обход: start считается с единицы, каждая страница —
        # отдельный платный запрос, поэтому выходим сразу, как набрали нужное.
        start = 1
        while len(candidates) < wanted and start <= MAX_TOTAL:
            page_size = min(MAX_PER_PAGE, wanted - len(candidates))
            payload = await self._request(query.text, start=start, num=page_size)
            if payload is None:
                break

            items = payload.get("items") or []
            if not items:
                break

            for item in items:
                if not isinstance(item, dict):
                    continue
                candidate = self._to_candidate(item)
                if candidate is None or candidate.raw_id in seen:
                    continue
                seen.add(candidate.raw_id or "")
                candidates.append(candidate)

            # Выдача кончилась раньше запрошенного — дальше страниц нет
            if len(items) < page_size:
                break
            start += page_size

        return candidates[:wanted]

    async def health(self) -> tuple[bool, str]:
        """Пробный поиск на одну ссылку.

        Проверка намеренно настоящая, хотя и тратит один платный запрос из
        суточной сотни: непустой ключ ещё не значит работающий — ровно на этом
        в проекте уже обжигались с ключом Яндекса, который был мёртв полтора
        месяца, а доктор показывал «есть».
        """
        if not (self._api_key and self._cse_id):
            return False, "нет GOOGLE_API_KEY или GOOGLE_CSE_ID"
        probe = SearchQuery(
            text="catalyst deactivation research",
            source_type=SourceType.WEB,
            lang="en",
            rationale="health probe",
        )
        try:
            found = await self.search(probe, limit=1)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if not found:
            return False, "ключ принят, но выдача пуста — проверьте режим «искать по всему вебу»"
        return True, f"ок, ссылок в пробе: {len(found)}, израсходован 1 платный запрос"

    async def _request(self, text: str, *, start: int, num: int) -> dict[str, Any] | None:
        """Свой GET вместо _get_json: у Google говорящие коды отказа."""
        params = {
            "key": self._api_key,
            "cx": self._cse_id,
            "q": text,
            "num": str(num),
            "start": str(start),
        }
        if self._lang_restrict:
            params["lr"] = self._lang_restrict

        await self._throttle()
        try:
            response = await self._client.get(API_URL, params=params)
            self.queries_used += 1
            if response.status_code in (403, 429):
                log.warning(
                    "google_search: квота исчерпана или ключ не принят (%s) — источник пропущен: %s",
                    response.status_code,
                    _reason(response),
                )
                return None
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("google_search не ответил: %s", exc)
            return None
        return payload if isinstance(payload, dict) else None

    def _to_candidate(self, item: dict[str, Any]) -> Candidate | None:
        title = _clean(item.get("title"))
        url = _clean(item.get("link"))
        if not title or not url:
            return None

        try:
            return Candidate(
                source_type=SourceType.WEB,
                title=title,
                url=url,
                # Домен — самая честная догадка о том, чья это разработка:
                # mit.edu говорит больше, чем название страницы
                org=_clean(item.get("displayLink")) or None,
                year=_year_from(item),
                snippet=_clean(item.get("snippet"))[:MAX_SNIPPET],
                # Ключ склейки — нормализованный адрес, а не название: у разных
                # страниц заголовки совпадают сплошь и рядом («Технологии»,
                # «Разработки»), и склейка по названию потеряла бы половину
                # находок. Тот же приём нужен в yandex_search на этапе Э2.
                raw_id=_normalize_url(url),
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("google_search: запись отброшена (%s): %s", url, exc)
            return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return _WS.sub(" ", str(value)).strip()


def _normalize_url(url: str) -> str:
    """Свести адрес к виду, одинаковому для дублей.

    Отбрасываем схему, www, метки кампаний в запросе и хвостовой слэш: одна и та
    же страница приходит из Google и Яндекса в разном написании.
    """
    try:
        parts = urlsplit(url.strip().lower())
    except ValueError:
        return url.strip().lower()
    host = parts.netloc.removeprefix("www.")
    path = parts.path.rstrip("/")
    return urlunsplit(("", host, path, "", "")).lstrip("/")


def _year_from(item: dict[str, Any]) -> int | None:
    """Достать год публикации из микроразметки страницы, если она есть."""
    pagemap = item.get("pagemap")
    if not isinstance(pagemap, dict):
        return None
    metatags = pagemap.get("metatags")
    if not isinstance(metatags, list) or not metatags:
        return None
    head = metatags[0]
    if not isinstance(head, dict):
        return None
    for key in _DATE_KEYS:
        if (match := _YEAR.search(_clean(head.get(key)))) is not None:
            return int(match.group(0))
    return None


def _reason(response: httpx.Response) -> str:
    """Вытащить пояснение Google из тела отказа: «dailyLimitExceeded» и прочее."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return _clean(error.get("message")) or str(error.get("code", ""))
    return ""
