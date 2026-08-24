"""OpenAlex — русскоязычный научный поиск и обогащение метаданных по DOI.

ИСТОЧНИК
    OpenAlex, открытый каталог научных работ, авторов и организаций.
    API: https://api.openalex.org/works
    Lookup по DOI: https://api.openalex.org/works/doi:10.1016/j.example.2024.01

У АДАПТЕРА ДВЕ РОЛИ, И ОНИ НЕ ПЕРЕСЕКАЮТСЯ
    1. ПОИСК (Т-15, РЕШ-71) — `search`. Появился после того, как живой прогон
       16 августа 2026 дал `cyberleninka: 0`: OAI-PMH КиберЛенинки в принципе не
       поисковый интерфейс, и чинить его нечем
       (`.planning/evidence/E2A-ru-science.md`, §1). OpenAlex на том же запросе
       («гидроочистка дизельного топлива») с `filter=language:ru` находит русские
       статьи по теме, включая попадание 2023 года с DOI и ссылкой на PDF.
       Русскоязычный корпус — 3,4 млн работ, из них с 2021 года 420 тыс. с
       рефератом и 481 тыс. с DOI. Ключа поиск не требует, `Disallow` у источника
       нет; вежливый режим — параметр `mailto` с почтой владельца, как у Crossref.
    2. ОБОГАЩЕНИЕ ПО DOI (Т-3, РЕШ-50) — `enrich_by_doi`. Список DOI приходит от
       любого источника, а у OpenAlex добирается то, чего у него нет:
       нормализованные организации (institutions с ROR), цитируемость,
       ссылка на открытый полный текст.

    Поведение обогащения задачей Т-15 НЕ МЕНЯЛОСЬ ни в одной строке, включая
    требование ключа для lookup и заголовок `Authorization`. Поиск ходит своим
    путём (`_fetch_search`), обогащение — своим (`_lookup`); общего у них только
    троттлинг и разбор инвертированного индекса.

ПОЧЕМУ ИСТОЧНИК БОЛЬШЕ НЕ `requires_key` И НЕ ТОЛЬКО ДЛЯ ОБОГАЩЕНИЯ
    Прежняя редакция (РЕШ-10) запрещала поиск как платный и включала адаптер
    только при заполненном `OPENALEX_API_KEY`. С обоими флагами поиск был
    недостижим: пометка «только обогащение» заставляла оркестратор пропускать
    источник, а `requires_key` не давал ему попасть в сборку вовсе. РЕШ-71
    отменяет запрет, поэтому `requires_key = False` (ключ поиску не нужен), а
    пометка снята — вместе с ветвью, которая её читала (Т-25).
    Ключ остался нужен обогащению — это проверяется внутри `enrich_by_doi`, и
    без ключа оно просто пропускается, не задевая поиск.

ПОИСК КЛЮЧА НЕ ТРЕБУЕТ, НО ТАРИФИЦИРУЕТСЯ (Т-18)
    Живая анонимная проба 17.08.2026 (без ключа, только `mailto`) ответила 200
    и вернула тарифные заголовки: `X-RateLimit-Cost-USD: 0.001`,
    `X-RateLimit-Limit-USD: 0.1`, `X-RateLimit-Remaining-USD: 0.099`. То есть
    поисковый запрос стоит $0,001, а суточная бесплатная норма — $0,10, ровно
    сто запросов. Это меняет не право искать (РЕШ-10 отменён), а обязанность
    считать: расход обязан быть виден оператору, иначе прогон незаметно
    выедает дневную норму. Счётчик ведётся в `search_queries_used`, остаток
    берётся из заголовка ответа, а строку для оператора собирает
    `search_usage_note()`.

    В `Report.cost_rub` эти доллары НЕ подмешиваются: там рубли моделей, и
    смешение двух валют в одном поле хуже отсутствия счётчика. Расход поиска —
    отдельная строка.

    Lookup обогащения (`_lookup`) к этому счётчику отношения не имеет: он
    бесплатен и считается своим числом запросов внутри `enrich_by_doi`.

ЧТО ЗАБИРАЕТСЯ ИЗ ОТВЕТА (задача Т-3, РЕШ-50)
    * аннотация (`abstract_inverted_index`) → `Candidate.snippet`;
    * организация первого автора (`authorships[].institutions[].display_name`)
      → `Candidate.org`;
    * ссылка на открытый полный текст (`best_oa_location.pdf_url`)
      → `Candidate.pdf_url`;
    * цитируемость (`cited_by_count`) — отдельной строкой во вход скоринга.
    Адаптер отдаёт ПАТЧ, а не кандидата: обогащение не создаёт новых находок, а
    решение «чем можно заполнить пустое поле» принимает оркестратор.

АЛЬТЕРНАТИВА БЕЗ ДЕНЕГ
    Полный снапшот выкладывается в S3 и качается бесплатно, без ключа:
        aws s3 sync --no-sign-request s3://openalex/ ./openalex-snapshot/
    Это ~400 ГБ сжатого JSONL. Имеет смысл, если понадобится массовая
    аналитика по организациям РФ, а не точечное обогащение.

НАСТРОЙКИ
    settings.openalex_api_key — необязателен: без него работает поиск, а
    обогащение по DOI пропускается с записью в лог.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import date
from typing import Any

import httpx

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, SearchError
from app.search.crossref import CONTACT_EMAIL
from app.search.openalex_parse import (
    MAX_SNIPPET,
    _abstract_from_inverted_index,
    normalize_doi,
    payload_to_patch,
    work_to_candidate,
)

log = logging.getLogger(__name__)

__all__ = [
    "API_URL",
    "BATCH_SIZE",
    "FREE_DAILY_USD",
    "FRESHNESS_YEARS",
    "MAX_SNIPPET",
    "USD_PER_SEARCH",
    "OpenAlexAdapter",
    "_abstract_from_inverted_index",
    "normalize_doi",
]

API_URL = "https://api.openalex.org/works"
#: Вежливый режим OpenAlex — та же почта владельца, что и у Crossref. Берётся
#: импортом, а не второй копией строки: контакт владельца обязан быть один.
USER_AGENT = f"TechMostScout/1.0 (mailto:{CONTACT_EMAIL})"
#: Свежесть выдачи поиска. Пять лет — ровно та граница, на которой разведка
#: проверила источник живьём (`from_publication_date:2021-01-01` в 2026 году).
FRESHNESS_YEARS = 5
#: Цена одного ПОИСКОВОГО запроса в долларах. Не оценка: заголовок
#: `X-RateLimit-Cost-USD` живой анонимной пробы 17.08.2026 вернул ровно 0.001.
USD_PER_SEARCH = 0.001
#: Суточная бесплатная норма (`X-RateLimit-Limit-USD` той же пробы) — сто
#: поисковых запросов в сутки. Ключ на неё не влияет, она считается по mailto.
FREE_DAILY_USD = 0.1
#: Заголовки ответа с остатком нормы: доллары и число запросов
REMAINING_USD_HEADER = "X-RateLimit-Remaining-USD"
REMAINING_CALLS_HEADER = "X-RateLimit-Remaining"
#: Потолок `per-page` у OpenAlex
MAX_PER_PAGE = 200
#: select экономит трафик: без него на каждую работу приезжают десятки
#: килобайт концептов, тем и списков цитирований, которые нам не нужны.
SEARCH_SELECT_FIELDS = (
    "id,display_name,publication_year,doi,type,primary_location,best_oa_location,abstract_inverted_index,authorships"
)
#: Сколько DOI обрабатывается между паузами. Lookup идёт по одному запросу на
#: DOI (это бесплатный путь), батч — единица ритма, а не один общий запрос.
BATCH_SIZE = 50
#: Пауза между батчами: источник бесплатный, но долбить его пачкой из сотен
#: запросов подряд невежливо и быстро приводит к 429.
BATCH_PAUSE_SECONDS = 0.2
#: Пауза между отдельными lookup ВНУТРИ батча. Без неё обещание «не долбить
#: пачкой» держалось на одной паузе между батчами, а сами 50 запросов уходили
#: подряд, за доли секунды: `_throttle` базового адаптера при
#: `min_interval_seconds <= 0` возвращается сразу и был чистой декорацией (П-4
#: аудита). 0,05 с растягивает батч из 50 запросов на 2,5 с — контракт SPEC §4
#: (батч 50, пауза 0,2 с между батчами) при этом не меняется.
MIN_INTERVAL_SECONDS = 0.05


class OpenAlexAdapter(SearchAdapter):
    """Русскоязычный научный поиск плюс обогащение метаданных по DOI."""

    name = "openalex"
    source_types = (SourceType.PUBLICATION,)
    #: Поиск бесплатен и ключа не требует (РЕШ-71), поэтому источник включается
    #: всегда. Ключ нужен только обогащению и проверяется в `enrich_by_doi`.
    requires_key = False
    implemented = True
    #: Пауза между отдельными запросами — иначе `_throttle` ничего не делает
    min_interval_seconds = MIN_INTERVAL_SECONDS

    def __init__(self, client: httpx.AsyncClient, *, api_key: str | None = None) -> None:
        super().__init__(client)
        self._api_key = api_key or None
        #: Почему последний обход оборвался на середине. Заполняется, когда
        #: часть патчей уже собрана и отдаётся, а обход прерван отказом:
        #: возвращаемый словарь такой отказ не выражает никак, а прогон обязан
        #: оставить след (П-4, П-5 аудита). None — обход прошёл целиком.
        self.last_error: str | None = None
        #: Сколько ПОИСКОВЫХ запросов ушло за жизнь адаптера. Поиск ключа не
        #: требует, но тарифицируется ($0,001 за запрос при суточной норме
        #: $0,10), поэтому расход обязан быть посчитан и показан оператору.
        self.search_queries_used = 0
        #: Остаток суточной нормы из заголовков последнего ответа. None —
        #: источник его не сообщил (или поиска в этом прогоне не было вовсе):
        #: подставлять сюда расчётное значение нельзя, это было бы выдумкой.
        self.daily_remaining_usd: float | None = None
        self.daily_remaining_calls: int | None = None

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        """Найти работы по формулировке, отфильтровав их по языку и свежести.

        Фильтр языка — существо задачи Т-15, а не украшение: без `language:ru`
        взвешенный поиск OpenAlex отдаёт по русскому запросу преимущественно
        англоязычные работы, и русскоязычной научной выдачи в отчёте снова не
        появляется. Язык берётся из самой формулировки (`SearchQuery.lang`),
        поэтому англоязычный запрос плана получит `language:en` — прибивать
        фильтр к «ru» значило бы молча подменять то, что попросил план.

        Ключ здесь не участвует: поиск бесплатен, вежливость обеспечивает
        `mailto` с почтой владельца (и в параметрах, и в `User-Agent`, — часть
        прокси срезает заголовок, как и у Crossref).

        Транспортный отказ поднимается как `SearchError`: инвариант №5 в
        редакции РЕШ-43. Пустая выдача при успешном ответе — обычный пустой
        список, не отказ.
        """
        params = {
            "search": query.text,
            "filter": f"language:{query.lang},from_publication_date:{self._from_publication_date()}",
            "per-page": str(max(1, min(limit, MAX_PER_PAGE))),
            "select": SEARCH_SELECT_FIELDS,
            "mailto": CONTACT_EMAIL,
        }
        payload = await self._fetch_search(params, query.text)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            log.warning("openalex: ответ без поля results на «%s»", query.text)
            return []

        candidates: list[Candidate] = []
        for work in results:
            if not isinstance(work, dict):
                continue
            candidate = work_to_candidate(work, provider=self.name)
            if candidate is not None:
                candidates.append(candidate)
        return candidates[:limit]

    async def _fetch_search(self, params: dict[str, str], text: str) -> dict | None:
        """GET к поиску OpenAlex. Отдельно от `_lookup`, чтобы правка поиска
        физически не могла задеть обогащение по DOI: у ролей разные адреса,
        разные заголовки и разные условия отказа.

        Счётчик растёт сразу после того, как ответ получен, — до проверки кода
        ответа: источник тарифицирует принятый запрос, а не удачный, и 429 с
        500 стоят ровно столько же. Не выросшим счётчик остаётся только там,
        где ответа не было вовсе (обрыв соединения, таймаут).
        """
        await self._throttle()
        try:
            response = await self._client.get(
                API_URL, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"openalex не ответил на поиск «{text}»: {exc}") from exc
        self.search_queries_used += 1
        self._remember_quota(response.headers)
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(f"openalex не ответил на поиск «{text}»: {exc}") from exc
        return payload if isinstance(payload, dict) else None

    def _remember_quota(self, headers: Any) -> None:
        """Запомнить остаток суточной нормы из заголовков ответа.

        Заголовков может не быть (прокси срезал, источник поменял схему) —
        тогда прежнее значение не трогается и остаётся None: «не знаю» честнее
        придуманного остатка.
        """
        with suppress(TypeError, ValueError):
            self.daily_remaining_usd = float(headers.get(REMAINING_USD_HEADER))
        with suppress(TypeError, ValueError):
            self.daily_remaining_calls = int(headers.get(REMAINING_CALLS_HEADER))

    @property
    def search_cost_usd(self) -> float:
        """Во сколько обошёлся поиск за этот прогон. Доллары, не рубли: в
        `Report.cost_rub` они не подмешиваются (границы Т-18)."""
        return round(self.search_queries_used * USD_PER_SEARCH, 4)

    def search_usage_note(self) -> str:
        """Строка расхода поиска для оператора в конце `scout run`.

        Из неё обязано быть видно три вещи: источник платный, у него есть
        суточная норма и сколько её осталось. Остаток берётся из заголовка
        ответа, а не считается: норма общая на все прогоны за сутки, и
        вычитание своих запросов из неё врало бы после второго запуска.
        """
        note = (
            f"OpenAlex — платный поиск: запросов {self.search_queries_used}, "
            f"${self.search_cost_usd:.3f} по ${USD_PER_SEARCH:.3f} за запрос; "
            f"суточная бесплатная норма ${FREE_DAILY_USD:.2f}"
        )
        if self.daily_remaining_usd is None:
            return note + ", остаток источник не сообщил"
        note += f", осталось ${self.daily_remaining_usd:.3f}"
        if self.daily_remaining_calls is not None:
            note += f" ({self.daily_remaining_calls} запросов)"
        return note

    @staticmethod
    def _from_publication_date() -> str:
        return f"{date.today().year - FRESHNESS_YEARS}-01-01"

    async def enrich_by_doi(self, dois: list[str]) -> dict[str, dict[str, Any]]:
        """Добрать по списку DOI организацию, аннотацию, PDF и цитируемость.

        Возвращает отображение «нормализованный DOI → патч метаданных». DOI, по
        которому в каталоге ничего нет, в ответе просто отсутствует.

        Ключа нет — пустой словарь, запись в лог и ни одного обращения в сеть:
        обогащение необязательно, ронять из-за него прогон нечестно.

        Транспортный отказ (HTTP-ошибка, 429, нечитаемое тело) поднимается как
        `SearchError` — инвариант №5 в редакции РЕШ-43: адаптер говорит «не
        смог», а решение «прогон из-за этого не останавливать» принимает
        оркестратор. Гасить отказ здесь в пустой успех запрещено.

        Отказ в СЕРЕДИНЕ обхода — другой случай: собранные патчи остаются и
        возвращаются, обход прекращается, а причина уходит в `last_error` и в
        лог на `WARNING`. Пустым успехом это не становится: при отказе на
        первом же DOI собирать нечего, и наружу уходит `SearchError`.
        """
        if not self._api_key:
            log.warning("openalex: обогащение пропущено: нет OPENALEX_API_KEY")
            return {}

        wanted: list[str] = []
        seen: set[str] = set()
        for raw in dois:
            doi = normalize_doi(raw)
            if doi and doi not in seen:
                seen.add(doi)
                wanted.append(doi)
        if not wanted:
            return {}

        patches: dict[str, dict[str, Any]] = {}
        requests = 0
        self.last_error = None
        for start in range(0, len(wanted), BATCH_SIZE):
            if start:
                await self._pause_between_batches()
            for doi in wanted[start : start + BATCH_SIZE]:
                try:
                    payload = await self._lookup(doi)
                except SearchError as exc:
                    if not patches:
                        # Ничего собрать не успели — это отказ источника
                        # целиком, и он обязан дойти до оркестратора как отказ
                        # (инвариант №5 в редакции РЕШ-43).
                        raise
                    # Отказ в середине обхода: один 429 на пятидесятом DOI не
                    # причина выбрасывать сорок девять уже собранных патчей
                    # (П-4 аудита). Обход прекращается, частичный результат
                    # отдаётся, а причина остаётся видимой — и в логе, и в
                    # `last_error` для журнала стадий.
                    self.last_error = f"обход прерван на DOI {doi}: {exc}"
                    break
                requests += 1
                if payload is None:
                    continue
                patch = payload_to_patch(payload)
                if patch:
                    patches[doi] = patch
            if self.last_error:
                break
        if self.last_error:
            log.warning(
                "openalex: %s; сохранено патчей: %s, проверено DOI: %s из %s",
                self.last_error,
                len(patches),
                requests,
                len(wanted),
            )
        # Lookup по DOI бесплатен и безлимитен, поэтому счётчик — не деньги, а
        # только объём работы; в Report.cost_rub он ничего не добавляет.
        log.info(
            "openalex: обогащено %s DOI из %s за %s бесплатных lookup-запросов",
            len(patches),
            len(wanted),
            requests,
        )
        return patches

    async def health(self) -> tuple[bool, str]:
        """Проба для `scout doctor` — бесплатный поиск, роль которого работает
        всегда.

        Пробовать здесь lookup по DOI нельзя: он требует ключа, а источник
        теперь включается и без него (РЕШ-71). Доктор докладывал бы «нет ключа»
        про источник, который в этом же прогоне исправно ищет. Состояние
        обогащения при этом не скрывается — оно уходит второй половиной строки.
        """
        probe = SearchQuery(
            text="катализатор",
            source_type=SourceType.PUBLICATION,
            lang="ru",
            rationale="проба доктора",
        )
        try:
            found = await self.search(probe, limit=1)
        except SearchError as exc:
            return False, str(exc)
        detail = f"ок, кандидатов в пробе: {len(found)}"
        if not self._api_key:
            detail += "; обогащение по DOI выключено: нет OPENALEX_API_KEY"
        return True, detail

    async def _pause_between_batches(self) -> None:
        """Пауза между батчами. Отдельным методом — чтобы ритм обхода можно было
        проверить тестом, не засыпая в нём по-настоящему."""
        await asyncio.sleep(BATCH_PAUSE_SECONDS)

    async def _lookup(self, doi: str) -> dict[str, Any] | None:
        """Один бесплатный lookup. None — работы нет в каталоге (404), это не отказ."""
        await self._throttle()
        try:
            response = await self._client.get(
                f"{API_URL}/doi:{doi}",
                headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(f"openalex не ответил на lookup DOI {doi}: {exc}") from exc
        return payload if isinstance(payload, dict) else None
