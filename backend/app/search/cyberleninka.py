"""КиберЛенинка — единственный открытый российский источник публикаций.

Бесплатно, без ключа, доступна из РФ, отдаёт метаданные по OAI-PMH:
https://cyberleninka.ru/oai

Главное ограничение, из которого вырос весь дизайн этого адаптера: OAI-PMH не
умеет полнотекстовый поиск по произвольной фразе. Есть только «выдай записи»
(ListRecords) с фильтрами по набору (set) и дате (from/until). Поэтому:

1. Тянем пачку записей ListRecords + metadataPrefix=oai_dc (+ set/from);
2. Фильтруем локально — по вхождению ключевых слов запроса в dc:title и
   dc:description, регистронезависимо, по нормализованным словам;
3. Кэшируем ответы на диск: ListRecords тяжёлый (десятки тысяч записей,
   секунды на страницу), а набор данных меняется медленно — TTL 7 дней;
4. resumptionToken поддерживаем, но не больше 3 страниц за один вызов search:
   иначе один запрос пайплайна утащит весь репозиторий.

ЧТО ПОКАЗАЛА ЖИВАЯ ПРОВЕРКА (важно для ожиданий от источника)

* Страница ListRecords отдаёт всего 10 записей, размер задаёт сервер — то есть
  за один вызов search мы просматриваем окно в 30 записей из ~2 млн. Без набора
  (set) это случайные записи (в выдаче шло литературоведение), и совпадений по
  техническому запросу почти наверняка не будет. Вывод: КиберЛенинка работает
  только с осмысленным set, см. ниже.
* В реальных записях НЕТ ни dc:description, ни dc:date — приходят только title,
  creator, type, publisher, identifier. Поэтому фильтруем фактически по
  названию, а year обычно остаётся None. Подставлять сюда datestamp из header
  нельзя: это дата попадания записи в репозиторий, а не год публикации, и в
  отчёте это было бы прямой неправдой.
* Наборов (ListSets) — около 3930, и среди них есть предметные, а не только
  по журналам. Полезные: cas (Chemical Abstracts), agris (сельхоз и пищевые
  технологии), pubmed / im / medline (медицина), georef (геология), zbmath,
  rsci / rinc / vak (уровень издания), а также journal_<id> под конкретный
  журнал (например journal_1426 — «Уголь»). Список наборов можно получить
  методом list_sets() и держать сопоставление «отрасль → set» в data/.
* set сужает первую страницу как надо (по cas приходит химия и биотех), но
  вторая и третья страницы по resumptionToken съезжают на другие темы — похоже,
  токен КиберЛенинки не сохраняет набор. Со стороны клиента это не лечится.

ЧЕСТНЫЙ ВЫВОД ПРО ЭТОТ ИСТОЧНИК
  Через OAI-PMH попадание по конкретному техническому запросу — редкая удача,
  и на живых запросах («катализатор гидроочистки», set=cas) мы получили ноль.
  Держать адаптер стоит как дешёвый и легальный канал к российским статьям, но
  рассчитывать в отчёте надо на Crossref. Если КиберЛенинка понадобится как
  полноценный поиск, единственный путь — её собственный поиск по сайту, а это
  парсинг HTML: отдельное решение, отдельные риски, отдельный этап.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date
from pathlib import Path

import httpx
from defusedxml import ElementTree as ET  # noqa: N817 — ET общепринятое сокращение; defusedxml защищает от XML-атак
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, SearchError

log = logging.getLogger(__name__)

BASE_URL = "https://cyberleninka.ru/oai"
DEFAULT_CACHE_DIR = Path("out/cache/cyberleninka")
CACHE_TTL_SECONDS = 7 * 24 * 3600
#: Сервер отдаёт по 10 записей на страницу, размер страницы не настраивается —
#: значит окно просмотра за один вызов search всего MAX_PAGES * 10 записей
MAX_PAGES = 3
RECORDS_PER_PAGE = 10
#: from фильтрует по datestamp (когда запись попала в репозиторий), не по году статьи
FRESHNESS_YEARS = 5
MAX_SNIPPET = 700
MAX_AUTHORS = 12

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_WORD = re.compile(r"[\w\-]+", re.UNICODE)
_WS = re.compile(r"\s+")
#: Обрезаем слово до основы: русская морфология иначе не даст совпадений
_STEM_LEN = 6
_STOPWORDS = frozenset(
    {
        "для",
        "при",
        "что",
        "как",
        "или",
        "это",
        "его",
        "уже",
        "без",
        "над",
        "под",
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "are",
        "was",
    }
)


class CyberLeninkaAdapter(SearchAdapter):
    """Публикации КиберЛенинки через OAI-PMH с локальной фильтрацией."""

    name = "cyberleninka"
    source_types = (SourceType.PUBLICATION,)
    requires_key = False
    #: Репозиторий небыстрый — не долбим его чаще раза в секунду
    min_interval_seconds = 1.0

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        oai_set: str | None = None,
        cache_dir: Path | str | None = None,
    ) -> None:
        super().__init__(client)
        self._oai_set = oai_set
        self._cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        keywords = _keywords(query.text)
        if not keywords:
            return []
        needed = max(1, (len(keywords) + 1) // 2)

        params: dict[str, str] = {
            "verb": "ListRecords",
            "metadataPrefix": "oai_dc",
            "from": f"{date.today().year - FRESHNESS_YEARS}-01-01",
        }
        if self._oai_set:
            params["set"] = self._oai_set
        else:
            # Без набора мы просматриваем случайные MAX_PAGES * 10 записей из
            # ~2 млн — попадание по техническому запросу крайне маловероятно
            log.warning(
                "cyberleninka: набор (set) не задан, просматриваем лишь %s случайных записей — "
                "укажите oai_set (например cas, agris, georef), иначе источник почти бесполезен",
                MAX_PAGES * RECORDS_PER_PAGE,
            )

        scored: list[tuple[int, Candidate]] = []
        for page in range(MAX_PAGES):
            body = await self._fetch(params)
            root = _parse(body)
            if root is None:
                break
            if (error := root.find("oai:error", NS)) is not None:
                # noRecordsMatch — нормальный ответ, а не авария
                log.info("cyberleninka: OAI-ошибка %s", error.get("code"))
                break

            for record in root.iterfind(".//oai:record", NS):
                scored_item = self._to_scored(record, keywords, needed)
                if scored_item is not None:
                    scored.append(scored_item)

            if len(scored) >= limit:
                break
            token = _resumption_token(root)
            if not token:
                break
            # По спецификации resumptionToken несовместим с остальными фильтрами
            params = {"verb": "ListRecords", "resumptionToken": token}
            log.debug("cyberleninka: страница %s, накоплено %s", page + 2, len(scored))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [candidate for _, candidate in scored[:limit]]

    async def health(self) -> tuple[bool, str]:
        """Пробуем verb=Identify: он дешёвый, в отличие от ListRecords."""
        try:
            await self._throttle()
            response = await self._client.get(BASE_URL, params={"verb": "Identify"})
            response.raise_for_status()
            root = _parse(response.text)
        except (httpx.HTTPError, ValueError) as exc:
            return False, f"OAI недоступен: {exc}"
        if root is None:
            return False, "OAI ответил не XML"
        node = root.find(".//oai:repositoryName", NS)
        return True, f"ок, репозиторий: {(node.text or '').strip() if node is not None else '?'}"

    async def list_sets(self) -> dict[str, str]:
        """Наборы репозитория: setSpec → человеческое название.

        Нужно, чтобы подобрать set под отрасль (их около 3930: предметные вроде
        cas и agris, признаки уровня издания вроде vak и rsci, и journal_<id>
        под конкретный журнал). Результат стоит сохранить в data/ и не дёргать
        этот метод в пайплайне — ответ большой.
        """
        body = await self._fetch({"verb": "ListSets"})
        root = _parse(body)
        if root is None:
            return {}
        sets: dict[str, str] = {}
        for node in root.iterfind(".//oai:set", NS):
            spec = node.find("oai:setSpec", NS)
            name = node.find("oai:setName", NS)
            if spec is not None and spec.text:
                sets[spec.text.strip()] = (name.text or "").strip() if name is not None else ""
        return sets

    # --- сеть и кэш -----------------------------------------------------

    async def _fetch(self, params: dict[str, str]) -> str:
        """GET к OAI-PMH с диск-кэшем. Транспортный отказ поднимается как
        SearchError — _safe_search оркестратора поймает его и честно отметит
        источник недоступным (инвариант №5), вместо того чтобы страница молча
        считалась последней."""
        cached = self._read_cache(params)
        if cached is not None:
            return cached
        await self._throttle()
        try:
            response = await self._client.get(BASE_URL, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SearchError(f"cyberleninka не ответила на {params.get('verb')}: {exc}") from exc
        self._write_cache(params, response.text)
        return response.text

    def _cache_path(self, params: dict[str, str]) -> Path:
        digest = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
        return self._cache_dir / f"{digest}.json"

    def _read_cache(self, params: dict[str, str]) -> str | None:
        path = self._cache_path(params)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - float(payload.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
            return None
        body = payload.get("body")
        return body if isinstance(body, str) else None

    def _write_cache(self, params: dict[str, str], body: str) -> None:
        path = self._cache_path(params)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"fetched_at": time.time(), "params": params, "body": body}),
                encoding="utf-8",
            )
        except OSError as exc:  # кэш — оптимизация, его отказ не ломает поиск
            log.debug("cyberleninka: кэш не записан (%s): %s", path, exc)

    # --- разбор ---------------------------------------------------------

    def _to_scored(self, record: ET.Element, keywords: list[str], needed: int) -> tuple[int, Candidate] | None:
        header = record.find("oai:header", NS)
        if header is not None and header.get("status") == "deleted":
            return None

        title = _text(record, "dc:title")
        # dc:description в реальных записях почти всегда отсутствует, поэтому
        # фактически совпадение ищется по названию и издателю
        description = _text(record, "dc:description")
        publisher = _text(record, "dc:publisher")
        if not title:
            return None

        matched = _match_score(keywords, f"{title} {description} {publisher}")
        if matched < needed:
            return None

        url = _identifier_url(record)
        if not url:
            return None

        try:
            candidate = Candidate(
                source_type=SourceType.PUBLICATION,
                title=title,
                url=url,
                org=publisher or None,
                authors=_texts(record, "dc:creator")[:MAX_AUTHORS],
                year=_year(_text(record, "dc:date")),
                # Аннотации нет — показываем хотя бы издателя, чтобы карточка
                # не пришла в отчёт совсем пустой
                snippet=(description or publisher)[:MAX_SNIPPET],
                raw_id=url,
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("cyberleninka: запись отброшена (%s): %s", url, exc)
            return None
        return matched, candidate


def _parse(body: str) -> ET.Element | None:
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        log.warning("cyberleninka: XML не разобран: %s", exc)
        return None


def _resumption_token(root: ET.Element) -> str:
    node = root.find(".//oai:resumptionToken", NS)
    return (node.text or "").strip() if node is not None else ""


def _texts(record: ET.Element, path: str) -> list[str]:
    values: list[str] = []
    for node in record.iterfind(f".//oai_dc:dc/{path}", NS):
        cleaned = _WS.sub(" ", node.text or "").strip()
        if cleaned:
            values.append(cleaned)
    return values


def _text(record: ET.Element, path: str) -> str:
    values = _texts(record, path)
    return values[0] if values else ""


def _identifier_url(record: ET.Element) -> str:
    """dc:identifier бывает несколько; ссылка на статью — та, что http(s)."""
    for value in _texts(record, "dc:identifier"):
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _year(value: str) -> int | None:
    """Год из dc:date. Обычно его нет — тогда None.

    Сознательно не берём datestamp из header: это дата появления записи в
    репозитории, и статья 1998 года превратилась бы в отчёте в свежую.
    """
    match = re.search(r"(19|20)\d{2}", value)
    return int(match.group(0)) if match else None


def _keywords(text: str) -> list[str]:
    words = [word.lower() for word in _WORD.findall(text)]
    return [word for word in words if len(word) >= 3 and word not in _STOPWORDS]


def _match_score(keywords: list[str], haystack: str) -> int:
    lowered = haystack.lower()
    return sum(1 for word in keywords if word[:_STEM_LEN] in lowered)
