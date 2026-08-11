"""Яндекс — открытый веб в двух контурах: российском и мировом.

ЗАЧЕМ ЭТОТ ИСТОЧНИК
    Значительная часть нужного нам живёт только на сайтах и не имеет API:
    страницы лабораторий и кафедр, центры трансфера технологий (ЦТТ), реестры
    поддержанных проектов РНФ и Фонда содействия инновациям, новости о пилотных
    внедрениях, отчёты по НИОКР. Без веб-поиска эти следы не найти.

ДВА КОНТУРА ОДНИМ КЛЮЧОМ (РЕШ-14)
    `SEARCH_TYPE_RU`  — выдача yandex.ru: русскоязычные сайты вузов, ЦТТ, фонды.
    `SEARCH_TYPE_COM` — выдача yandex.com: мировой веб.

    Мировой контур проверен живым вызовом 11 августа 2026 на английском
    запросе: вернулись репозитории Манчестера, Åbo Akademi, Университета
    Альберты, Aston, PubMed Central, Taylor & Francis, ResearchGate — восемь
    доменных зон. То есть это настоящая международная выдача, а не российская с
    английскими словами.

    Режим выбирается по языку формулировки, а не по настройке: планировщик и
    так строит план из русских и английских запросов (`Brief.keywords_en`), и
    каждый уходит в свой контур. Отдельного ключа для мира не нужно.

    Почему не Google, за которым сюда шли изначально: Custom Search JSON API
    закрыт для новых клиентов, режим «искать по всему вебу» больше не создаётся,
    а сам API отключается 1 января 2027. Подробности — в
    `backend/app/search/google_search.py` и `docs/deps-manual.md`, пункт 7.

ЦЕНА И ПОТОЛОК
    Yandex Search API платный, бесплатного тарифа нет: порядка 50–200 ₽ за 1000
    запросов. Поэтому у адаптера жёсткий предел `max_queries` на прогон
    пайплайна (по умолчанию шесть). Исчерпан — источник молча отдаёт пустой
    список и пишет в лог, а не разоряет владельца на длинном плане поиска.

КАК УСТРОЕН ВЫЗОВ
    Поиск асинхронный: POST searchAsync возвращает операцию, результат
    забирается опросом по её идентификатору и приходит как XML в base64.
    Синхронный /v2/web/search существует, но у него жёстче лимит частоты.

    Структура ответа сверена с фактическим XML, а не с документацией:
        yandexsearch/response/results/grouping/group/doc
        doc: url, domain, title (с подсветкой в <hlword>), modtime, passages,
             properties/lang
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx
from defusedxml import ElementTree as ET  # noqa: N817 — ET общепринятое сокращение; defusedxml защищает от XML-атак
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, normalize_url

log = logging.getLogger(__name__)

SEARCH_ASYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations"

#: Язык формулировки → контур выдачи
SEARCH_TYPE_BY_LANG = {"ru": "SEARCH_TYPE_RU", "en": "SEARCH_TYPE_COM"}

#: Контур выдачи → метка для счётчика кандидатов в оркестраторе (РЕШ-31).
#: Метка живёт рядом с правилом, которое выбирает сам контур, — так они не
#: могут разъехаться, а оркестратору не нужен частный случай под этот источник.
CONTOUR_LABEL_BY_SEARCH_TYPE = {"SEARCH_TYPE_RU": "yandex-ru", "SEARCH_TYPE_COM": "yandex-com"}

#: Яндекс отдаёт не больше сотни на страницу; нам столько и не нужно
MAX_PER_PAGE = 100
MAX_SNIPPET = 700

#: Где искать гранты: у реестров РНФ и Фонда содействия инновациям нет API, и
#: сузить выдачу до них — единственный способ вытащить поддержанные проекты.
#: Ограничение применяется ТОЛЬКО к российскому контуру и ТОЛЬКО к запросам про
#: гранты: для остальных типов `site:` отрезал бы всё живое, ради чего и нужен
#: открытый веб. Список держим здесь, а не в data/: две записи, и они следуют из
#: смысла источника, а не из подкрутки качества.
GRANT_SITES = ("rscf.ru", "fasie.ru")


class YandexSearchAdapter(SearchAdapter):
    """Поиск по открытому вебу: российский и мировой контуры одним ключом."""

    name = "yandex_search"
    source_types = (SourceType.WEB,)
    #: Бесплатного тарифа нет — без ключа и каталога адаптер не включается
    requires_key = True
    #: Запросы платные и асинхронные; частота ограничена не жёстко
    min_interval_seconds = 0.5

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str | None = None,
        folder_id: str | None = None,
        max_queries: int = 6,
        poll_interval_seconds: float = 2.0,
        max_wait_seconds: float = 60.0,
    ) -> None:
        super().__init__(client)
        self._api_key = api_key or None
        self._folder_id = folder_id or None
        self._max_queries = max_queries
        self._poll_interval = poll_interval_seconds
        self._max_wait = max_wait_seconds
        #: Сколько платных запросов израсходовано за прогон
        self.queries_used = 0

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        if not (self._api_key and self._folder_id):
            log.warning("yandex_search: нет ключа или идентификатора каталога")
            return []
        if self.queries_used >= self._max_queries:
            log.warning(
                "yandex_search: предел %s платных запросов на прогон исчерпан — формулировка «%s» пропущена",
                self._max_queries,
                query.text,
            )
            return []

        search_type = self._search_type(query)
        text = _with_site_filter(query, search_type)

        raw = await self._run_search(text, search_type=search_type, limit=limit)
        if raw is None:
            return []
        return self._parse(raw, limit=limit)

    @staticmethod
    def _search_type(query: SearchQuery) -> str:
        """Контур выдачи по языку формулировки — единственное место правила."""
        return SEARCH_TYPE_BY_LANG.get(query.lang, "SEARCH_TYPE_RU")

    def contour_label(self, query: SearchQuery) -> str:
        """Метка контура для оркестратора: `yandex-ru` или `yandex-com`.

        Берёт тот же контур, что реально уйдёт в `search()` — метка и правило
        выбора не могут разойтись, потому что вычисляются одной функцией.
        """
        return CONTOUR_LABEL_BY_SEARCH_TYPE[self._search_type(query)]

    async def health(self) -> tuple[bool, str]:
        """Пробный поиск в мировом контуре.

        Проверка настоящая и стоит одного платного запроса: непустой ключ ещё
        не значит работающий — на ключе Яндекса проект это уже проходил, когда
        доктор полтора месяца показывал «есть» у мёртвого ключа.
        """
        if not (self._api_key and self._folder_id):
            return False, "нет YANDEX_SEARCH_API_KEY или YANDEX_CATALOG_ID"
        probe = SearchQuery(
            text="catalyst deactivation research",
            source_type=SourceType.WEB,
            lang="en",
            rationale="health probe",
        )
        try:
            found = await self.search(probe, limit=3)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if not found:
            return False, "ключ принят, но выдача пуста — проверьте биллинг и права сервисного аккаунта"
        domains = {c.org for c in found if c.org}
        return True, f"ок, мировой контур: ссылок {len(found)}, доменов {len(domains)}, 1 платный запрос"

    async def _run_search(self, text: str, *, search_type: str, limit: int) -> bytes | None:
        """Запустить асинхронный поиск и дождаться результата."""
        body = {
            "query": {"searchType": search_type, "queryText": text},
            "folderId": self._folder_id,
            "responseFormat": "FORMAT_XML",
            "groupSpec": {"groupsOnPage": str(max(1, min(limit, MAX_PER_PAGE)))},
        }
        headers = {"Authorization": f"Api-Key {self._api_key}", "Content-Type": "application/json"}

        await self._throttle()
        try:
            response = await self._client.post(SEARCH_ASYNC_URL, headers=headers, json=body)
            self.queries_used += 1
            response.raise_for_status()
            operation_id = response.json().get("id")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("yandex_search: поиск не запустился (%s): %s", search_type, exc)
            return None
        if not operation_id:
            log.warning("yandex_search: ответ без идентификатора операции")
            return None

        return await self._await_operation(operation_id, headers)

    async def _await_operation(self, operation_id: str, headers: dict[str, str]) -> bytes | None:
        """Опрашивать операцию, пока не завершится. Молча сдаться по времени."""
        deadline = asyncio.get_running_loop().time() + self._max_wait
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self._poll_interval)
            try:
                got = await self._client.get(f"{OPERATION_URL}/{operation_id}", headers=headers)
                got.raise_for_status()
                payload = got.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("yandex_search: операция %s не опрашивается: %s", operation_id, exc)
                return None
            if not payload.get("done"):
                continue
            if (error := payload.get("error")) is not None:
                log.warning("yandex_search: операция завершилась ошибкой: %s", str(error)[:200])
                return None
            raw = (payload.get("response") or {}).get("rawData")
            if not raw:
                log.warning("yandex_search: операция завершилась без данных")
                return None
            try:
                return base64.b64decode(raw)
            except (ValueError, TypeError) as exc:
                log.warning("yandex_search: результат не декодируется: %s", exc)
                return None

        log.warning("yandex_search: операция %s не завершилась за %s с", operation_id, self._max_wait)
        return None

    def _parse(self, raw: bytes, *, limit: int) -> list[Candidate]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            log.warning("yandex_search: XML не разобран: %s", exc)
            return []

        # Яндекс сообщает о своих отказах внутри XML, а не кодом HTTP:
        # исчерпанный лимит, отсутствие прав, пустая выдача — всё сюда
        for error in root.iter("error"):
            log.warning("yandex_search: отказ поиска, код %s: %s", error.get("code"), (error.text or "")[:200])
            return []

        candidates: list[Candidate] = []
        seen: set[str] = set()
        for doc in root.findall(".//doc"):
            candidate = self._to_candidate(doc)
            if candidate is None or candidate.raw_id in seen:
                continue
            seen.add(candidate.raw_id or "")
            candidates.append(candidate)
            if len(candidates) >= limit:
                break
        return candidates

    def _to_candidate(self, doc: Any) -> Candidate | None:
        url = (doc.findtext("url") or "").strip()
        title = _text_of(doc.find("title"))
        if not url or not title:
            return None

        passages = doc.find("passages")
        snippet = _text_of(passages) if passages is not None else _text_of(doc.find("headline"))

        try:
            return Candidate(
                source_type=SourceType.WEB,
                title=title,
                url=url,
                # Домен — самая честная догадка о том, чья это разработка:
                # pure.manchester.ac.uk говорит больше, чем заголовок страницы
                org=(doc.findtext("domain") or "").strip() or None,
                # modtime — время правки страницы, а не публикации работы.
                # Другого сигнала у веб-страницы нет, поэтому берём его, но
                # понимаем: свежая правка старой страницы даст свежий год.
                year=_year_from_modtime(doc.findtext("modtime")),
                snippet=snippet[:MAX_SNIPPET],
                raw_id=normalize_url(url),
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("yandex_search: запись отброшена (%s): %s", url, exc)
            return None


def _with_site_filter(query: SearchQuery, search_type: str) -> str:
    """Добавить операторы `site:` там, где это осмысленно.

    Только российский контур и только гранты: реестры РНФ и Фонда содействия
    инновациям иначе не достать. Для всего остального ограничение по сайтам
    убило бы смысл открытого веба.
    """
    if search_type != "SEARCH_TYPE_RU" or query.source_type is not SourceType.GRANT:
        return query.text
    sites = " | ".join(f"site:{site}" for site in GRANT_SITES)
    return f"{query.text} ({sites})"


def _text_of(node: Any) -> str:
    """Собрать текст узла вместе с подсветкой: Яндекс режет слова на <hlword>.

    Куски склеиваются через пробел, а не встык: соседние <passage> — это разные
    предложения, и без разделителя получалось «первый фрагмент.Второй фрагмент».
    Лишние пробелы схлопывает нормализация ниже, поэтому подсветка внутри слова
    от этого не страдает.
    """
    if node is None:
        return ""
    return " ".join(" ".join(node.itertext()).split())


def _year_from_modtime(value: str | None) -> int | None:
    """modtime приходит как 20190627T045553 — год это первые четыре знака."""
    if not value or len(value) < 4:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None
