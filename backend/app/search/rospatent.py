"""Роспатент — поисковая платформа ФИПС. Патенты и полезные модели.

ЗАЧЕМ ЭТОТ ИСТОЧНИК
    Патент — самое сильное подтверждение, что разработка существует и у неё
    есть конкретный правообладатель (то есть с кем разговаривать заказчику).
    Заявитель патента приходит прямо полем `applicant` — это и есть `org`
    кандидата, не догадка по домену и не аффилиация первого автора.

ДВА РАЗНЫХ ДОСТУПА, НЕ ПУТАТЬ
    1. Открытые данные Роспатента — https://rospatent.gov.ru/opendata
       Бесплатно, без ключа, но наборы выкладываются файлами: своя выкачка,
       свой локальный индекс, десятки гигабайт. Это отдельная задача, кода
       здесь нет.
    2. Поисковая платформа — https://searchplatform.rospatent.gov.ru
       Полнотекстовый и семантический поиск, REST API, ключ по заявке через
       личный кабинет ФИПС. Ключ у проекта есть (РЕШ-69), поэтому адаптер
       работает именно через платформу.

    Отсюда `requires_key = True`: без ключа адаптер не собирается вовсе, как
    и остальные платные источники. Половинчатый режим «без ключа ищем по
    открытым данным» не реализован, и притворяться работающим он не должен.

ФОРМА ОТВЕТА (снята с живого вызова, а не с документации)
    POST /patsearch/v0.2/search, тело {"qn": <текст>, "limit": <сколько>},
    заголовок Authorization: Bearer <ключ>. Ответ:

        {"available":…, "total":…, "timings":…,
         "hits": [{"id": "WO2020130881A1_20200625",
                   "common": {"publishing_office": "WO",
                              "document_number": "2020130881",
                              "kind": "A1",
                              "publication_date": "2020.06.25",
                              "classification": {"ipc": [{"fullname": …}]}},
                   "biblio": {"ru": {"title":…, "applicant":[{"name":…}],
                                     "inventor":[{"name":…}]},
                              "en": {…}},
                   "snippet": {"title":…, "description":…, "lang":…,
                               "applicant":…}}]}

    В `snippet` совпадения подсвечены тегами <em>: в текст доказательства они
    попасть не должны — карточку читает человек, а не браузер.

ССЫЛКА НА ДОКУМЕНТ
    Платформа отдаёт метаданные, а не адрес читаемой человеком карточки.
    Ссылка собирается на Google Patents по номеру публикации
    (`WO2020130881A1`, `RU2799999C1`): тот же выбор уже сделан для патентов
    США в `patents_view.py` — реестр ФИПС по прямой ссылке открывается через
    сессию поиска, а Google Patents открывается всегда и всеми. Домен
    `patents.google.com` вдобавок значится в списке допустимых для типа
    `patent` в `prompts/verifier.md`.

ТЕМП
    Лимит на ключ платформой не опубликован, поэтому берём осторожно:
    не чаще одного запроса в секунду, как у OAI КиберЛенинки.

БЕДНАЯ ВЫДАЧА НА ПЕРЕУТОЧНЁННОЙ ФОРМУЛИРОВКЕ (диагноз З-6,
`.planning/evidence/E2J-rospatent-разбор.md`). Живой контрольный вызов
показал 1 кандидата на «способ ультразвуковой диагностики супоросности
свиноматок на основе нейросети» и 20 — на той же формулировке без хвоста
«на основе нейросети»: платформа теряет совпадения, когда в единственной
(по правилу промпта) патентной формулировке появляется редкий
технологический термин, которого нет в формальном языке патентных текстов.
Порог здесь не ноль, как у КиберЛенинки: переуточнение душит выдачу не до
нуля, а до одного-двух документов, поэтому повтор срабатывает при
`< 3` кандидатов на исходной формулировке — тот же приём (ступенчатое
укорачивание, один повтор, без модели), что и в `cyberleninka.py`.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter, SearchError
from app.search.query_shortening import shorten_ru_query

log = logging.getLogger(__name__)

OPENDATA_URL = "https://rospatent.gov.ru/opendata"
SEARCH_PLATFORM_URL = "https://searchplatform.rospatent.gov.ru/patsearch/v0.2/search"
#: Читаемая человеком карточка документа по номеру публикации
DOCUMENT_URL = "https://patents.google.com/patent/{number}"
#: Почта владельца на случай, если настройки не переданы: адаптер обязан
#: представляться контактом в любом запуске, а не только в полном пайплайне.
CONTACT_EMAIL = "uklonskiy.k@gmail.com"
#: Больше платформа за один запрос всё равно не отдаёт
MAX_LIMIT = 100
MAX_SNIPPET = 700
MAX_AUTHORS = 12
#: Порог повтора (диагноз З-6): переуточнение душит выдачу не до нуля, как у
#: КиберЛенинки, а до одного-двух документов — поэтому порог не `== 0`.
MIN_CANDIDATES_BEFORE_REPEAT = 3
#: Сколько содержательных слов оставлять при укорачивании — тот же потолок,
#: что и у КиберЛенинки (З-2): подобран не под один пример, а как разумный
#: компромисс между специфичностью и полнотой выдачи.
#: Патентная формулировка всегда русская (правило промпта: «на patent ровно
#: 1 запрос, и он на русском»), поэтому английский вариант укорачивания
#: (`query_shortening.english_content_words`) здесь не нужен.
SHORT_QUERY_WORDS = 4

#: Подсветка совпадений в snippet и прочая разметка
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_YEAR = re.compile(r"(19|20)\d{2}")
#: Номер публикации: код страны (2 буквы), числовая часть, код вида документа
#: (буква и обычно цифра — `B1`, `C1`, `A1`, `U1`). Шаблон нужен только затем,
#: чтобы аккуратно отделить числовую часть при снятии ведущих нулей — сам
#: собранный `number` (raw_id, dedup) им не трогаем.
_PUBLICATION_NUMBER = re.compile(r"^([A-Za-z]{2})(\d+)([A-Za-z][A-Za-z0-9]*)$")


class RospatentAdapter(SearchAdapter):
    """Патенты РФ и мировые публикации через поисковую платформу ФИПС."""

    name = "rospatent"
    source_types = (SourceType.PATENT,)
    #: Поисковая платформа работает только по ключу
    requires_key = True
    #: Адаптер рабочий: build_adapters включает его при заполненном ключе
    implemented = True
    #: Лимит на ключ не опубликован — держим один запрос в секунду
    min_interval_seconds = 1.0
    #: Ответ структурирован, разбирается кодом: нормализовать моделью нечего
    needs_extraction = False

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str | None = None,
        contact_email: str | None = None,
    ) -> None:
        super().__init__(client)
        self._api_key = api_key or None
        self._contact_email = contact_email or CONTACT_EMAIL

    @property
    def user_agent(self) -> str:
        """Честное представление источнику: кто стучится и куда писать."""
        return f"TechMostScout/1.0 (mailto:{self._contact_email})"

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        """Кандидаты по одной формулировке. Отказ источника — `SearchError`.

        Диагноз З-6 (`.planning/evidence/E2J-rospatent-разбор.md`): редкий
        технологический термин в единственной патентной формулировке душит
        выдачу платформы до одного-двух документов. При `< 3` кандидатов на
        исходной формулировке — один повтор с укороченной формой (первые 3–4
        содержательных слова, без модели). Больше одного повтора не делаем.
        """
        if not self._api_key:
            log.warning("rospatent: нет ROSPATENT_API_KEY — источник пропущен")
            return []

        text = query.text.strip()
        if not text:
            return []
        wanted = max(1, min(limit, MAX_LIMIT))

        candidates = await self._search_once(text, limit=wanted)

        short_text = _shorten_query(text)
        if len(candidates) < MIN_CANDIDATES_BEFORE_REPEAT and short_text and short_text != text:
            short_candidates = await self._search_once(short_text, limit=wanted)
            log.info(
                "rospatent: «%s» → кандидатов=%s; повтор укороченной «%s» → кандидатов=%s",
                text,
                len(candidates),
                short_text,
                len(short_candidates),
            )
            return short_candidates[:wanted]

        log.info("rospatent: «%s» → кандидатов=%s", text, len(candidates))
        return candidates[:wanted]

    async def _search_once(self, text: str, *, limit: int) -> list[Candidate]:
        """Один запрос к платформе, разбор `hits` в кандидатов."""
        payload = await self._request(text, limit=limit)
        hits = payload.get("hits")
        if not isinstance(hits, list):
            # Ответ разобран, записей нет — это пустой результат, а не отказ
            log.warning("rospatent: ответ без hits по запросу «%s»", text)
            return []

        candidates: list[Candidate] = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            candidate = self._to_candidate(hit)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def health(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "не заполнено: ROSPATENT_API_KEY"
        return await super().health()

    async def _request(self, text: str, *, limit: int) -> dict[str, Any]:
        """POST к поисковой платформе.

        Транспортный отказ, 401 и 429 поднимаются как `SearchError` — его ловит
        `_safe_search` оркестратора и честно помечает источник недоступным
        (инвариант №5 в редакции РЕШ-43). Тихий пустой список здесь недопустим:
        мёртвый ключ выглядел бы как «патентов по задаче нет».
        """
        await self._throttle()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        try:
            response = await self._client.post(SEARCH_PLATFORM_URL, json={"qn": text, "limit": limit}, headers=headers)
            # Значение ключа в сообщение не попадает (инвариант №3) — только
            # имя переменной окружения, по которому его чинить.
            if response.status_code in (401, 403):
                raise SearchError(f"rospatent: ключ не принят ({response.status_code}), проверьте ROSPATENT_API_KEY")
            if response.status_code == 429:
                raise SearchError("rospatent: превышен лимит запросов на ключ (429)")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(f"rospatent не ответил на «{text}»: {exc}") from exc
        if not isinstance(payload, dict):
            raise SearchError(f"rospatent: ответ не разобран как объект JSON по запросу «{text}»")
        return payload

    def _to_candidate(self, hit: dict[str, Any]) -> Candidate | None:
        common = _mapping(hit.get("common"))
        biblio = _mapping(hit.get("biblio"))
        ru = _mapping(biblio.get("ru"))
        en = _mapping(biblio.get("en"))
        snippet = _mapping(hit.get("snippet"))

        # Русское название важнее английского: отчёт читает русскоязычный
        # заказчик. Подсвеченный snippet — только как последняя подстраховка.
        title = _clean(ru.get("title")) or _clean(en.get("title")) or _strip_markup(snippet.get("title"))
        number = _document_number(common) or _clean(hit.get("id")).split("_")[0]
        if not title or not number:
            # Долг-2 З-6: раньше это был debug, и слепое пятно «что именно
            # отсеялось» закрывалось только живым контрольным вызовом
            # (`.planning/evidence/E2J-rospatent-разбор.md`, шаг 3). На info
            # причина отсева видна прямо в логе прогона.
            reason = (
                "нет ни названия, ни номера"
                if not title and not number
                else ("нет названия" if not title else "нет номера")
            )
            log.info("rospatent: запись отброшена — %s (id=%s)", reason, _clean(hit.get("id")) or "?")
            return None

        # Заявитель — прямое поле правообладателя, ради него источник и нужен
        org = _first_name(ru.get("applicant")) or _first_name(en.get("applicant")) or _clean(snippet.get("applicant"))

        try:
            return Candidate(
                source_type=SourceType.PATENT,
                title=title,
                url=DOCUMENT_URL.format(number=_normalize_document_number(number)),
                org=org or None,
                authors=_names(ru.get("inventor")) or _names(en.get("inventor")),
                year=_year(common.get("publication_date")),
                # Фрагмент описания без подсветки — это и есть текст, которым
                # карточка потом подтверждает утверждения
                snippet=_strip_markup(snippet.get("description"))[:MAX_SNIPPET],
                raw_id=number,
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("rospatent: запись %s отброшена: %s", number, exc)
            return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean(value: Any) -> str:
    return _WS.sub(" ", str(value)).strip() if isinstance(value, str) else ""


def _strip_markup(value: Any) -> str:
    """Снять подсветку <em> и прочие теги, развернуть HTML-сущности."""
    raw = value if isinstance(value, str) else ""
    if not raw:
        return ""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", raw))).strip()


def _document_number(common: dict[str, Any]) -> str:
    """Номер публикации целиком: страна + номер + вид документа.

    Одного `document_number` мало: номер уникален внутри ведомства, и без
    префикса страны заявка WO склеилась бы с чужой RU в `dedup_key`.
    """
    office = _clean(common.get("publishing_office"))
    number = _clean(common.get("document_number")).replace(" ", "")
    if not number:
        return ""
    kind = _clean(common.get("kind"))
    return f"{office}{number}{kind}"


def _normalize_document_number(number: str) -> str:
    """Нормализовать номер публикации по коду офиса (РЕШ-209).

    Общее снятие ведущих нулей (РЕШ-208) поменяло одну 404-ссылку на другую:
    по первоисточнику проверено (повторный аудит), что верный формат числовой
    части у Google Patents для евразийских публикаций — **шесть** знаков с
    ведущими нулями (`EA026963B1` — HTTP 200, заголовок страницы дословно
    совпал с названием карточки; `EA26963B1` без добивки и исходный
    `EA0000026963B1` — оба 404). Правило зависит от офиса:

    * `EA` — ведущие нули сначала снимаются целиком, чтобы найти значащие
      цифры (`EA0000026963B1` → «26963»), затем число добивается нулями до
      шести знаков (`026963`). Если значащих цифр уже шесть и больше —
      обрезать их нельзя (выдумывать формат для непредвиденной разрядности
      запрещено, критерий 3).
    * `RU` — российские номера приходят от платформы без ведущих нулей и
      Google Patents открывает их как есть; если платформа всё же пришлёт
      номер с нулями, они снимаются целиком — так же, как до РЕШ-209.
    * Остальные офисы (`BY`, `WO`, `US`, `SU` и любые другие) — номер
      отдаётся без изменений: для `BY`, например, при проверке по
      первоисточнику ни один из форматов Google Patents не открылся
      (РЕШ-209, вопрос владельцу без ответа) — трогать эти номера значит
      гадать вслепую.

    Номер, который не разобрался по шаблону «страна+цифры+вид», тоже
    отдаётся как есть — молча ломать ссылку нельзя, но и выдумывать
    структуру нельзя.
    """
    match = _PUBLICATION_NUMBER.match(number)
    if not match:
        return number
    country, digits, kind = match.groups()
    if country == "RU":
        return f"{country}{digits.lstrip('0') or '0'}{kind}"
    if country == "EA":
        significant = digits.lstrip("0") or "0"
        padded = significant.zfill(6) if len(significant) < 6 else significant
        return f"{country}{padded}{kind}"
    return number


def _first_name(value: Any) -> str:
    """Первый заявитель (или автор) списка — тот, с кем и разговаривать."""
    if not isinstance(value, list):
        return ""
    for element in value:
        name = _clean(element.get("name")) if isinstance(element, dict) else _clean(element)
        if name:
            return name
    return ""


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for element in value[:MAX_AUTHORS]:
        name = _clean(element.get("name")) if isinstance(element, dict) else _clean(element)
        if name:
            names.append(name)
    return names


def _year(value: Any) -> int | None:
    """Дата публикации приходит как «2020.06.25» — нужен только год."""
    match = _YEAR.search(_clean(value))
    return int(match.group(0)) if match else None


def _shorten_query(text: str) -> str:
    """Короткая форма формулировки — приём общий с КиберЛенинкой (Д-5, `query_shortening`).

    Живой контроль З-6 показал, что именно хвостовой технологический термин
    («на основе нейросети») душит выдачу платформы — укорачивание с начала
    формулировки его и отбрасывает.
    """
    return shorten_ru_query(text, word_limit=SHORT_QUERY_WORDS)
