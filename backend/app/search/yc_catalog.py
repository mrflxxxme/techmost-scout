"""Каталог Y Combinator — бесплатный источник зарубежных стартапов-аналогов.

Работает по типу `analog` (Э6-2б). У каталога нет поиска: открытое
зеркало `yc-oss.github.io` отдаёт одним файлом (≈10 МБ, около шести тысяч
компаний) всё, что опубликовано на ycombinator.com. Поэтому файл скачивается
ОДИН раз на экземпляр адаптера (экземпляр живёт один прогон), держится в
памяти урезанным до нужных полей, а поиск — совпадение слов формулировки с
именем, описаниями, тегами и отраслью.

Ссылка кандидата — страница компании на ycombinator.com, то есть первоисточник
записи каталога (инвариант №1); запись с адресом на любом другом узле
пропускается: зеркало не обязано отдавать только свои страницы.

Страна заполняется только при однозначном сопоставлении: все указанные места
компании (кроме «Remote») сводятся к одной стране из таблицы `COUNTRY_CODES`.
Место вне таблицы или две страны — страны нет, угадывать нельзя.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, SearchError
from app.search.crossref import CONTACT_EMAIL
from app.search.query_shortening import english_content_words

log = logging.getLogger(__name__)

CATALOG_URL = "https://yc-oss.github.io/api/companies/all.json"
YC_HOST = "ycombinator.com"
MAX_SNIPPET = 700
#: Длина основы слова при сравнении: «monitor», «monitoring» и «monitors»
#: сводятся к одной основе без словаря и без модели
STEM_LENGTH = 6
#: Вес совпадения по полям: название и однострочное описание значат больше,
#: чем случайное слово в длинном тексте
WEIGHT_NAME = 4
WEIGHT_SHORT = 3
WEIGHT_TAGS = 2
WEIGHT_LONG = 1

#: Названия стран из поля `all_locations` каталога → ISO 3166-1 alpha-2
COUNTRY_CODES: dict[str, str] = {
    "USA": "US",
    "United States": "US",
    "United Kingdom": "GB",
    "India": "IN",
    "Canada": "CA",
    "Mexico": "MX",
    "France": "FR",
    "Germany": "DE",
    "Nigeria": "NG",
    "Singapore": "SG",
    "Brazil": "BR",
    "Israel": "IL",
    "Colombia": "CO",
    "Indonesia": "ID",
    "Spain": "ES",
    "Sweden": "SE",
    "Argentina": "AR",
    "Australia": "AU",
    "Chile": "CL",
    "Netherlands": "NL",
    "United Arab Emirates": "AE",
    "Switzerland": "CH",
    "Denmark": "DK",
    "Peru": "PE",
    "Egypt": "EG",
    "Pakistan": "PK",
    "South Korea": "KR",
    "Vietnam": "VN",
    "Philippines": "PH",
    "Kenya": "KE",
    "Norway": "NO",
    "Malaysia": "MY",
    "Turkey": "TR",
    "Ireland": "IE",
    "Hong Kong": "HK",
    "China": "CN",
    "Poland": "PL",
    "Portugal": "PT",
    "Slovenia": "SI",
    "Panama": "PA",
    "Austria": "AT",
    "Ghana": "GH",
    "Saudi Arabia": "SA",
    "New Zealand": "NZ",
    "Finland": "FI",
    "Belgium": "BE",
    "Romania": "RO",
    "Senegal": "SN",
    "Morocco": "MA",
    "South Africa": "ZA",
    "Italy": "IT",
    "Estonia": "EE",
    "Ukraine": "UA",
    "Georgia": "GE",
    "Japan": "JP",
    "Czechia": "CZ",
    "Puerto Rico": "PR",
    "Uganda": "UG",
    "Greece": "GR",
    "Uruguay": "UY",
    "Croatia": "HR",
    "Bulgaria": "BG",
    "Costa Rica": "CR",
    "Jordan": "JO",
    "Thailand": "TH",
    "Nepal": "NP",
    "Bangladesh": "BD",
    "Russia": "RU",
}
#: Места без страны: удалённая работа не привязывает компанию к государству
_REMOTE = {"remote", "fully remote", "partly remote", "unspecified", ""}

_TOKEN = re.compile(r"[A-Za-z0-9]+")
_YEAR = re.compile(r"(\d{4})\s*$")
_WS = re.compile(r"\s+")


class YCCatalogAdapter(SearchAdapter):
    """Стартапы Y Combinator как зарубежные аналоги."""

    name = "yc_catalog"
    source_types = (SourceType.ANALOG,)
    requires_key = False
    needs_extraction = False

    def __init__(self, client: httpx.AsyncClient, *, contact_email: str | None = None) -> None:
        super().__init__(client)
        self._contact_email = contact_email or CONTACT_EMAIL
        self._companies: list[dict[str, Any]] | None = None
        self._load_lock = asyncio.Lock()

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        """Кандидаты по одной формулировке. Отказ источника — `SearchError`."""
        if query.lang == "ru":
            log.debug("yc_catalog: пропускаем русскую формулировку «%s» — каталог англоязычный", query.text)
            return []
        stems = _stems(english_content_words(query.text))
        if not stems:
            return []
        companies = await self._load()
        # Совпасть должны хотя бы два слова (или единственное, если оно одно):
        # одно общее слово вроде «platform» тянет за собой половину каталога
        need = min(2, len(stems))
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for position, company in enumerate(companies):
            score, matched = _score(company, stems)
            if matched >= need:
                scored.append((-score, position, company))
        scored.sort(key=lambda row: row[:2])
        candidates: list[Candidate] = []
        for _score_key, _position, company in scored:
            candidate = self._to_candidate(company)
            if candidate is not None:
                candidates.append(candidate)
            if len(candidates) >= max(1, limit):
                break
        log.info("yc_catalog: «%s» → %s совпадений", query.text, len(candidates))
        return candidates

    async def _load(self) -> list[dict[str, Any]]:
        """Каталог целиком: один запрос на экземпляр, дальше из памяти.

        Отказ не запоминается: следующий поиск попробует ещё раз. Транспортный
        отказ и нечитаемое тело — `SearchError` (инвариант №5).
        """
        async with self._load_lock:
            if self._companies is not None:
                return self._companies
            try:
                response = await self._client.get(
                    CATALOG_URL,
                    headers={
                        "User-Agent": f"TechMostScout/1.0 (mailto:{self._contact_email})",
                        "Accept": "application/json",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                raise SearchError(f"yc_catalog ответил HTTP {exc.response.status_code}") from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise SearchError(f"yc_catalog не ответил: {type(exc).__name__}") from exc
            if not isinstance(payload, list):
                raise SearchError("yc_catalog: каталог не список компаний")
            self._companies = [_slim(item) for item in payload if isinstance(item, dict)]
            log.info("yc_catalog: загружено компаний: %s", len(self._companies))
            return self._companies

    def _to_candidate(self, company: dict[str, Any]) -> Candidate | None:
        """Запись каталога → кандидат. Страница не на ycombinator.com — пропуск."""
        url = company["url"]
        name = company["name"]
        if not name or not _is_yc_page(url):
            return None
        parts = [company["one_liner"]]
        if company["batch"]:
            parts.append(f"набор Y Combinator: {company['batch']}")
        if company["status"]:
            parts.append(f"статус: {company['status']}")
        if company["locations"]:
            parts.append(f"места: {company['locations']}")
        if company["long_description"]:
            parts.append(company["long_description"])
        try:
            return Candidate(
                source_type=SourceType.ANALOG,
                title=name,
                url=url,
                org=name,
                year=_batch_year(company["batch"]),
                snippet="; ".join(part for part in parts if part)[:MAX_SNIPPET],
                raw_id=company["slug"] or None,
                provider=self.name,
                country=_country(company["locations"]),
            )
        except ValidationError as exc:
            log.debug("yc_catalog: запись %s отброшена: %s", url, exc)
            return None


def _slim(item: dict[str, Any]) -> dict[str, Any]:
    """Оставить из записи только нужное: каталог в памяти держится весь прогон."""

    def text(key: str) -> str:
        value = item.get(key)
        return _WS.sub(" ", value).strip() if isinstance(value, str) else ""

    def texts(key: str) -> list[str]:
        value = item.get(key)
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    return {
        "name": text("name"),
        "slug": text("slug"),
        "url": text("url"),
        "one_liner": text("one_liner"),
        "long_description": text("long_description"),
        "batch": text("batch"),
        "status": text("status"),
        "locations": text("all_locations"),
        "tags": " ".join([*texts("tags"), *texts("industries"), text("industry"), text("subindustry")]),
    }


def _stems(words: list[str]) -> list[str]:
    """Различные основы слов формулировки в порядке появления."""
    stems: list[str] = []
    for word in words:
        stem = word[:STEM_LENGTH]
        if stem not in stems:
            stems.append(stem)
    return stems


def _field_stems(text: str) -> set[str]:
    return {token.lower()[:STEM_LENGTH] for token in _TOKEN.findall(text) if len(token) >= 3}


def _score(company: dict[str, Any], stems: list[str]) -> tuple[int, int]:
    """Вес совпадений и число совпавших слов формулировки."""
    fields = (
        (_field_stems(company["name"]), WEIGHT_NAME),
        (_field_stems(company["one_liner"]), WEIGHT_SHORT),
        (_field_stems(company["tags"]), WEIGHT_TAGS),
        (_field_stems(company["long_description"]), WEIGHT_LONG),
    )
    score = 0
    matched = 0
    for stem in stems:
        weights = [weight for tokens, weight in fields if stem in tokens]
        if weights:
            matched += 1
            score += max(weights)
    return score, matched


def _is_yc_page(url: str) -> bool:
    """Страница компании на ycombinator.com (допустим www и поддомены)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and (host == YC_HOST or host.endswith("." + YC_HOST))


def _batch_year(batch: str) -> int | None:
    """Год набора из `Winter 2012`; иного вида (`Summer 2024`, `Unspecified`)."""
    match = _YEAR.search(batch)
    return int(match.group(1)) if match else None


def _country(locations: str) -> str | None:
    """Страна компании — только если все её места ведут в одну страну.

    Место вне таблицы — неоднозначность, а не повод промолчать о нём: без
    него компания могла бы оказаться в двух странах, и страна не ставится.
    """
    codes: set[str] = set()
    for place in locations.split(";"):
        tail = place.split(",")[-1].strip()
        if tail.lower() in _REMOTE:
            continue
        code = COUNTRY_CODES.get(tail)
        if code is None:
            return None
        codes.add(code)
    return codes.pop() if len(codes) == 1 else None
