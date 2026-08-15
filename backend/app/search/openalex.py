"""OpenAlex — только обогащение по DOI, никогда не поиск.

ИСТОЧНИК
    OpenAlex, открытый каталог научных работ, авторов и организаций.
    API: https://api.openalex.org/works
    Lookup по DOI: https://api.openalex.org/works/doi:10.1016/j.example.2024.01
    Ключ: https://openalex.org/settings/api

ЧТО ИЗМЕНИЛОСЬ В ФЕВРАЛЕ 2026 (главное, из-за чего адаптер не универсальный)
    * Ключ стал ОБЯЗАТЕЛЕН — анонимных запросов больше нет.
    * Polite pool и параметр mailto отменены; присылать mailto бессмысленно.
    * Появилась тарификация: search — $1 за 1000 вызовов, list/filter — $0.10
      за 1000 вызовов, бесплатно только $1 в сутки (то есть ~1000 полнотекстовых
      поисков в месяц на всю систему — на пайплайн этого не хватит).
    * Lookup одной работы по DOI остался БЕСПЛАТНЫМ и БЕЗЛИМИТНЫМ.

ОТСЮДА ПРАВИЛО
    OpenAlex в этой системе — слой обогащения, а не поиска. Список DOI мы
    получаем из Crossref (бесплатно и без лимита), а у OpenAlex добираем то,
    чего у Crossref нет: нормализованные организации (institutions с ROR),
    цитируемость (cited_by_count), ссылку на открытый полный текст.
    Полнотекстовый поиск через OpenAlex не делать НИКОГДА — это платный вызов
    search, который дублирует бесплатный Crossref (РЕШ-10).

    Отсюда же `enrich_only = True`: адаптер попадает в реестр источников, но
    оркестратор не зовёт его `search` ни на одной формулировке. Без этого флага
    источник обогащения оказался бы в `sources_unavailable` каждого отчёта —
    `search` бросает `NotImplementedError`, и клиенту сообщалось бы о «не
    ответившем» источнике, который никто и не спрашивал.

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
    settings.openalex_api_key — обязателен, без него обогащение пропускается
    (requires_key = True, см. build_adapters в app/search/__init__.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, SearchError

log = logging.getLogger(__name__)

API_URL = "https://api.openalex.org/works"
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
#: Аннотация в OpenAlex лежит инвертированным индексом; собранный текст режем
#: по той же границе, что и Crossref, — вход скоринга не резиновый.
MAX_SNIPPET = 700
#: Работа для пробы в `scout doctor`: широко известная и заведомо в каталоге.
HEALTH_DOI = "10.1038/s41586-021-03819-2"

_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:")


def normalize_doi(value: Any) -> str:
    """DOI в том виде, в каком он уходит в lookup: без схемы, в нижнем регистре.

    Пустая строка означает «это не DOI»: у патента или веб-страницы в `raw_id`
    лежит номер или адрес, и обогащать по нему нечего.
    """
    if not isinstance(value, str):
        return ""
    doi = value.strip().lower()
    for prefix in _DOI_PREFIXES:
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
            break
    doi = doi.strip()
    return doi if doi.startswith("10.") else ""


class OpenAlexAdapter(SearchAdapter):
    """Обогащение метаданных по DOI. Поиск через этот источник запрещён."""

    name = "openalex"
    source_types = (SourceType.PUBLICATION,)
    #: С февраля 2026 ключ обязателен
    requires_key = True
    implemented = True
    #: Источник только для обогащения: оркестратор не зовёт его `search`
    enrich_only = True
    #: Пауза между отдельными lookup — иначе `_throttle` ничего не делает
    min_interval_seconds = MIN_INTERVAL_SECONDS

    def __init__(self, client: httpx.AsyncClient, *, api_key: str | None = None) -> None:
        super().__init__(client)
        self._api_key = api_key or None
        #: Почему последний обход оборвался на середине. Заполняется, когда
        #: часть патчей уже собрана и отдаётся, а обход прерван отказом:
        #: возвращаемый словарь такой отказ не выражает никак, а прогон обязан
        #: оставить след (П-4, П-5 аудита). None — обход прошёл целиком.
        self.last_error: str | None = None

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        raise NotImplementedError(
            "openalex: полнотекстовый поиск платный ($1/1000) и дублирует Crossref — "
            "источник используется только для обогащения по DOI (РЕШ-10)"
        )

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
                patch = _to_patch(payload)
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
        """Проба для `scout doctor` — один бесплатный lookup, поиск не трогаем."""
        if not self._api_key:
            return False, "нет ключа OPENALEX_API_KEY"
        try:
            payload = await self._lookup(HEALTH_DOI)
        except SearchError as exc:
            return False, str(exc)
        if payload is None:
            # Ключ принят и источник ответил — пробной работы в каталоге нет.
            return True, "ок, lookup по DOI отвечает (пробная работа не найдена)"
        return True, "ок, lookup по DOI отвечает"

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


def _to_patch(payload: dict[str, Any]) -> dict[str, Any]:
    """Патч к найденному кандидату: только те поля, которые OpenAlex реально отдал.

    Пустых значений в патче не бывает: «организация не указана» здесь означает
    отсутствие ключа, а не строку «не указана» — иначе обогащение затирало бы
    честное «неизвестно» выдуманным содержимым (инвариант №1).
    """
    patch: dict[str, Any] = {}

    org = _first_institution(payload.get("authorships"))
    if org:
        patch["org"] = org

    snippet = _abstract_from_inverted_index(payload.get("abstract_inverted_index"))
    if snippet:
        patch["snippet"] = snippet

    location = payload.get("best_oa_location")
    if isinstance(location, dict):
        pdf_url = str(location.get("pdf_url") or "").strip()
        if pdf_url:
            patch["pdf_url"] = pdf_url

    cited = payload.get("cited_by_count")
    if isinstance(cited, int) and not isinstance(cited, bool) and cited >= 0:
        patch["cited_by_count"] = cited

    return patch


def _first_institution(authorships: Any) -> str:
    """Организация первого автора, у которого она вообще проставлена.

    Аффилиации OpenAlex нормализованы (у каждой есть ROR), поэтому они точнее
    сырых строк Crossref — но подставляются только в пустое поле кандидата.
    """
    if not isinstance(authorships, list):
        return ""
    for authorship in authorships:
        if not isinstance(authorship, dict):
            continue
        for institution in authorship.get("institutions") or []:
            if not isinstance(institution, dict):
                continue
            name = str(institution.get("display_name") or "").strip()
            if name:
                return name
    return ""


def _abstract_from_inverted_index(value: Any) -> str:
    """Собрать аннотацию из инвертированного индекса OpenAlex.

    В ответе лежит {"слово": [позиции]} — прямого текста аннотации у источника
    нет вовсе, издатели запрещают его отдавать целиком.
    """
    if not isinstance(value, dict) or not value:
        return ""
    positions: list[tuple[int, str]] = []
    for word, places in value.items():
        if not isinstance(places, list):
            continue
        for place in places:
            if isinstance(place, int) and not isinstance(place, bool):
                positions.append((place, str(word)))
    if not positions:
        return ""
    positions.sort()
    return " ".join(word for _, word in positions).strip()[:MAX_SNIPPET]
