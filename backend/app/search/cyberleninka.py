"""КиберЛенинка — единственный открытый российский источник публикаций.

Бесплатно, без ключа, доступна из РФ. Ищем через её собственный поисковый
JSON-API:

    POST https://cyberleninka.ru/api/search
    {"mode": "articles", "q": "<текст запроса>", "size": 20, "from": 0}

ПОЧЕМУ НЕ OAI-PMH (Т-16, РЕШ-68). Раньше адаптер ходил в
https://cyberleninka.ru/oai и на живых запросах давал ноль. Разведка
(.planning/evidence/E2A-ru-science.md, часть 1) разобрала причины, все
проверены живыми запросами:

1. фильтр `from = год − 5` в OAI-PMH — НИЖНЯЯ граница, а записи идут по
   возрастанию `datestamp`: курсор вставал на самую старую партию (одна пачка
   от 3 января 2021 года), дальше которой адаптер не уходил никогда;
2. `resumptionToken` теряет `set` — из 30 записей окна в наборе только 10;
3. в `oai_dc` нет ни реферата, ни года, ни ключевых слов — совпадение искалось
   по одному названию;
4. окно 30 записей на репозиторий в ~2 млн;
5. наборы OAI — это внешние индексы (`cas` = «попало в Chemical Abstracts»), а
   не отраслевые рубрики; справочник `data/cyberleninka_sets.yaml` опирался на
   неверное понимание наборов и вместе с OAI-путём удалён.

Причины 2–5 со стороны клиента неустранимы: OAI-PMH КиберЛенинки в принципе не
поисковый интерфейс. Поисковый API на том же запросе, где OAI дал ноль, даёт
`found = 719` — с рефератом (`annotation`), годом, журналом, авторами,
признаком ВАК и фрагментами полного текста (`ocr`). Поэтому здесь нет ни
`FRESHNESS_YEARS`, ни фильтра по дате в запросе: свежесть берётся из поля
`year` ответа и взвешивается стадией скоринга, а не отсекается на входе.

УСЛОВИЯ ДОСТУПА, КОТОРЫЕ НЕЛЬЗЯ СМЯГЧАТЬ (РЕШ-70). В `robots.txt`
КиберЛенинки есть `Disallow: /api/`. Владелец ознакомлен и принимает риск.
Бот-защиты, капчи и подписи запроса там нет — обходить нечего и ничего
обходить не нужно. Из этого следуют обязательные правила, и они закреплены
тестами:

* не чаще одного запроса в секунду (`min_interval_seconds = 1.0`);
* честный `User-Agent` с почтой владельца — мы называем себя, а не
  притворяемся браузером;
* ответы кладутся в диск-кэш, чтобы повторный прогон по той же формулировке
  вообще не трогал источник;
* никаких приёмов, маскирующих нас под браузер или обходящих защиту.

Отказ источника (HTTP-ошибка, 429, нечитаемое тело) поднимается как
`SearchError`: его ловит `_safe_search` оркестратора, и источник честно
попадает в «Не ответили источники» (инвариант №5, РЕШ-43). Пустая выдача при
успешном ответе — обычный пустой список, а не отказ.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, SearchError
from app.search.crossref import CONTACT_EMAIL

log = logging.getLogger(__name__)

API_URL = "https://cyberleninka.ru/api/search"
SITE_URL = "https://cyberleninka.ru"
#: Режим поиска: статьи. Другие режимы API нам не нужны.
SEARCH_MODE = "articles"
#: Называем себя честно и оставляем контакт владельца — та же почта, что в
#: polite pool Crossref. Один контакт на все источники: подменять его от
#: источника к источнику значит вести две правды о том, кто мы такие.
USER_AGENT = f"TechMostScout/1.0 (mailto:{CONTACT_EMAIL})"
DEFAULT_CACHE_DIR = Path("out/cache/cyberleninka")
#: Сутки, а не неделя как у прежнего OAI-кэша: там кэшировалась статическая
#: выгрузка репозитория, здесь — выдача поиска, в которую доезжают новые статьи.
CACHE_TTL_SECONDS = 24 * 3600
#: Потолок размера страницы: больше за раз просить незачем, лимиты API не
#: документированы и нагрузочно не проверялись
MAX_SIZE = 50
MAX_SNIPPET = 700
MAX_AUTHORS = 12
#: Сколько фрагментов полного текста склеивать, когда реферата нет
MAX_OCR_FRAGMENTS = 3
#: catalogs = [{"id": 8, "alias_name": "vak", "acronym": "ВАК"}, …]
VAK_ALIASES = frozenset({"vak", "вак"})

#: Совпадения в ответе подсвечены разметкой <b>…</b> — снимаем её
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_YEAR = re.compile(r"(19|20)\d{2}")


class CyberLeninkaAdapter(SearchAdapter):
    """Публикации КиберЛенинки через её поисковый JSON-API."""

    name = "cyberleninka"
    source_types = (SourceType.PUBLICATION,)
    requires_key = False
    #: Ходим в путь, закрытый robots.txt (РЕШ-70): не чаще раза в секунду
    min_interval_seconds = 1.0

    def __init__(self, client: httpx.AsyncClient, *, cache_dir: Path | str | None = None) -> None:
        """`cache_dir` — куда складывать ответы; None означает `out/cache/cyberleninka`."""
        super().__init__(client)
        self._cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        """Кандидаты по одной формулировке. Отказ источника — `SearchError`."""
        text = query.text.strip()
        if not text:
            return []
        payload = await self._fetch(text, size=max(1, min(limit, MAX_SIZE)))

        articles = payload.get("articles")
        if not isinstance(articles, list):
            # found = 0 приходит вообще без articles — это результат, не отказ
            log.info("cyberleninka: в ответе нет статей (found=%s)", payload.get("found"))
            return []

        candidates: list[Candidate] = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            candidate = self._to_candidate(article)
            if candidate is not None:
                candidates.append(candidate)
        return candidates[:limit]

    async def health(self) -> tuple[bool, str]:
        """Проверка для `scout doctor`: один дешёвый поиск на размер 1."""
        try:
            payload = await self._fetch("катализатор", size=1)
        except SearchError as exc:
            return False, f"поиск недоступен: {exc}"
        return True, f"ок, найдено по пробе: {payload.get('found', '?')}"

    # --- сеть и кэш -----------------------------------------------------

    async def _fetch(self, text: str, *, size: int) -> dict[str, Any]:
        """POST к поисковому API с диск-кэшем.

        Транспортный отказ (HTTP-ошибка, 429, нечитаемое тело) поднимается как
        `SearchError` — инвариант №5, РЕШ-43: источник обязан попасть в отчёт
        как недоступный, а не притвориться, что ничего не нашлось.

        Тело собирается вручную и кодируется в UTF-8 явно: кириллица в `q`,
        отправленная в другой кодировке, даёт честный HTTP 200 с `found: 0` —
        грабля, на которой разведка потеряла первые попытки (§1.6
        доказательства). `q` — строка; массивом API отвечает 400.
        """
        body = json.dumps({"from": 0, "mode": SEARCH_MODE, "q": text, "size": size}, ensure_ascii=False, sort_keys=True)
        raw = self._read_cache(body)
        from_network = raw is None
        if raw is None:
            await self._throttle()
            try:
                response = await self._client.post(
                    API_URL,
                    content=body.encode("utf-8"),
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise SearchError(f"cyberleninka не ответила на «{text}»: {exc}") from exc
            raw = response.text

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise SearchError(f"cyberleninka: ответ на «{text}» не разобран как JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SearchError(f"cyberleninka: ответ на «{text}» — не объект JSON")

        if from_network:
            self._write_cache(body, raw)
        return payload

    def _cache_path(self, body: str) -> Path:
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
        return self._cache_dir / f"{digest}.json"

    def _read_cache(self, body: str) -> str | None:
        path = self._cache_path(body)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - float(payload.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
            return None
        cached = payload.get("body")
        return cached if isinstance(cached, str) else None

    def _write_cache(self, body: str, raw: str) -> None:
        path = self._cache_path(body)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"fetched_at": time.time(), "request": body, "body": raw}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:  # кэш — оптимизация, его отказ не ломает поиск
            log.debug("cyberleninka: кэш не записан (%s): %s", path, exc)

    # --- разбор ---------------------------------------------------------

    def _to_candidate(self, article: dict[str, Any]) -> Candidate | None:
        title = _clean(article.get("name"))
        if not title:
            return None
        url = _article_url(article.get("link"))
        if not url:
            # Без ссылки кандидат бесполезен: карточку нечем подтвердить
            return None

        journal = _clean(article.get("journal"))
        # Реферат — главное, чего не было в OAI. Нет его — берём фрагменты
        # полного текста, они в ответе тоже подсвечены и тоже по делу.
        abstract = _clean(article.get("annotation")) or _ocr_fragments(article.get("ocr"))
        marks = [mark for mark in ("ВАК" if _has_vak(article.get("catalogs")) else "", journal) if mark]
        snippet = " — ".join(part for part in (" · ".join(marks), abstract) if part)[:MAX_SNIPPET]

        try:
            return Candidate(
                source_type=SourceType.PUBLICATION,
                title=title,
                url=url,
                org=journal or None,
                authors=_authors(article.get("authors")),
                year=_year(article.get("year")),
                snippet=snippet,
                raw_id=url,
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("cyberleninka: запись отброшена (%s): %s", url, exc)
            return None


def _clean(value: Any) -> str:
    """Снять подсветку `<b>…</b>`, развернуть HTML-сущности, схлопнуть пробелы.

    Теги удаляются, а не заменяются пробелом: подсветка бывает и внутри слова
    («гидро<b>очистк</b>и»), и пробел разорвал бы слово.
    """
    if not isinstance(value, str):
        return ""
    return _WS.sub(" ", html.unescape(_TAG.sub("", value))).strip()


def _article_url(value: Any) -> str:
    """`link` приходит относительным (`/article/n/…`) — достраиваем до полного."""
    link = _clean(value)
    if not link:
        return ""
    if link.startswith(("http://", "https://")):
        return link
    return f"{SITE_URL}/{link.lstrip('/')}"


def _authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [name for item in value[:MAX_AUTHORS] if (name := _clean(item))]


def _year(value: Any) -> int | None:
    """Год публикации из поля `year`: он приходит и числом, и строкой."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = _YEAR.search(value) if isinstance(value, str) else None
    return int(match.group(0)) if match else None


def _has_vak(value: Any) -> bool:
    """Есть ли среди каталогов статьи признак ВАК."""
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        marks = {_clean(item.get("alias_name")).lower(), _clean(item.get("acronym")).lower()}
        if marks & VAK_ALIASES:
            return True
    return False


def _ocr_fragments(value: Any) -> str:
    """Фрагменты полного текста — запасной реферат, когда `annotation` пуст."""
    if not isinstance(value, list):
        return ""
    fragments = [text for item in value[:MAX_OCR_FRAGMENTS] if (text := _clean(item))]
    return " … ".join(fragments)
