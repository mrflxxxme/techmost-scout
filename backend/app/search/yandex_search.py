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
    а сам API отключается 1 января 2027. Адаптер удалён (РЕШ-174), подробности —
    в `docs/sources.md` и `docs/deps-manual.md`, пункт 7.

ЦЕНА И ПОТОЛОК
    Yandex Search API платный, бесплатного тарифа нет: порядка 50–200 ₽ за 1000
    запросов. Поэтому у адаптера жёсткий предел `max_queries` на прогон
    пайплайна (по умолчанию шесть). Исчерпан — формулировка не отправляется, а
    источник отдаёт пустой список, вместо того чтобы разорять владельца на
    длинном плане поиска.

    Молчать об этом нельзя (Ц-1 пост-аудита Э2-Б). После второго прохода
    (РЕШ-81) плана хватает ровно на предел: два прохода по две web-формулировки
    и по одной конкурсной — это все шесть запросов, запаса не осталось. Раньше
    пропуск жил только в `log.warning`, а лога прогона снаружи не видно, и в
    отчёте исчерпанный платный предел выглядел так же, как «источник ответил и
    ничего не нашёл». Теперь каждая пропущенная формулировка ложится в
    `limit_problems`, а `gather_candidates` оркестратора забирает записи
    вызовом `drain_limit_problems` в `SearchOutcome.problems` — тем же путём,
    каким туда попадают деградации стадии extractor, и дальше в `error` записи
    стадии `search`.

    Вызов на стороне оркестратора — обязательная половина этого механизма, а не
    подробность: первый цикл правок завёл здесь и список, и метод, но ни разу
    их не спросил, и наружу по-прежнему не доходило ничего (Ц-1 пост-аудита
    Э2-Б). Сторожит проводку целиком `tests/test_yandex_limit_visibility.py`
    (адаптер) вместе с `tests/test_run_accounting.py` (прогон).

    Пропуск по пределу — не отказ источника: `SearchError` не поднимается и в
    `sources_unavailable` Яндекс не уезжает. Источник исправно ответил на всё
    оплаченное, кончился наш бюджет, и отчёт обязан называть именно эту причину.

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
from app.search.base import SearchAdapter, SearchError, normalize_url

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
#: Ограничение применяется ТОЛЬКО к российскому контуру и ТОЛЬКО к
#: web-формулировкам с грантовым намерением (маркеры — GRANT_INTENT_MARKERS
#: ниже): для остального `site:` отрезал бы всё живое, ради чего и нужен
#: открытый веб. Список держим здесь, а не в data/: две записи, и они следуют из
#: смысла источника, а не из подкрутки качества.
GRANT_SITES = ("rscf.ru", "fasie.ru")

#: Признак грантового намерения в русской web-формулировке (РЕШ-39, РЕШ-41).
#: Раньше сужение включалось по типу запроса `grant`, которого планировщик
#: больше не выдаёт: состав типов промпта = состав рабочих адаптеров, а
#: рабочего адаптера под `grant` нет и не было. Гранты не пропадают как задача
#: поиска — они становятся web-запросами, и признак сужения переехал на текст
#: формулировки. Владелец подтвердил ровно этот способ различения в составе
#: задачи Ф-3 (`.planning/PLAN.md`).
GRANT_INTENT_MARKERS = ("грант", "поддержанн", "фонд содействия", "рнф", "конкурс")

#: Площадки конкурсов и хакатонов (РЕШ-79 п. 3, РЕШ-87). Своего адаптера под
#: конкурсы нет и не будет: российские площадки либо не отвечают снаружи
#: (`hacks-ai.ru` — 504, `leaders-of-digital.ru` — таймаут, `all-cups.ru` —
#: ошибка TLS), либо закрыли свой API для роботов (`codenrock.com`), поэтому
#: единственный доступный путь к ним — чужой поисковый индекс
#: (`.planning/evidence/contests-recon.md` §1.5).
#:
#: `devpost.com` стоит здесь ТОЛЬКО как строка `site:devpost.com` внутри
#: поисковой формулировки Яндекса. Прямое обращение к `devpost.com/api/*`
#: запрещено (РЕШ-87), хотя эндпоинт открыт и отвечает без ключа: мы читаем
#: чужой поисковый индекс, а не роботим их сайт.
#:
#: Сужение обязательное, а не по маркерам в тексте (в отличие от грантов):
#: конкурсной формулировку делает именно список площадок, без него запрос
#: вырождается в обычный веб-поиск и тратит тот же платный лимит впустую.
CONTEST_SITES = (
    "hacks-ai.ru",
    "leaders-of-digital.ru",
    "all-cups.ru",
    "codenrock.com",
    "upgreat.one",
    "sk.ru",
    "devpost.com",
)

#: Якоря итогов конкурса (РЕШ-107, РЕШ-117): за пять платных прогонов Э2-Б
#: единственная конкурсная формулировка ВСЕГДА сужалась по CONTEST_SITES и
#: приносила только рекламные страницы площадок ("Как выиграть хакатон",
#: "Организация хакатонов под ключ"), 0 карточек при 20 кандидатах на прогон.
#: Формулировка с любым из этих якорей ищет уже готовое решение и разбор
#: итогов - заметку о победителе, которая чаще лежит на сайте вуза или в
#: отраслевом издании, которых в CONTEST_SITES не будет никогда, поэтому она
#: уходит БЕЗ сужения (см. _with_site_filter, has_contest_outcome_marker).
#:
#: Основы, а не словоформы - "победител" ловит "победители", "победителя";
#: "призёр" и "призер" - обе орфографии, с буквой ё и без неё. Этот же кортеж
#: использует app.pipeline.stages.normalize_contest_queries (РЕШ-121): план
#: строит модель, и опираться на её послушание нельзя - код гарантирует, что
#: формулировка с этими якорями в плане ровно одна.
CONTEST_OUTCOME_MARKERS = (
    "победител",
    "призёр",
    "призер",
    "итоги хакатона",
    "проект команды",
)


class YandexSearchAdapter(SearchAdapter):
    """Поиск по открытому вебу: российский и мировой контуры одним ключом."""

    name = "yandex_search"
    #: `CONTEST` здесь не украшение: оркестратор отдаёт адаптеру только те
    #: формулировки, чей тип перечислен в `source_types`, и без этой строки
    #: конкурсный запрос молча не доходил бы ни до одного источника
    #: (урок `JOURNAL.md:177`). Своего адаптера под конкурсы нет — площадки
    #: читаются через тот же контур Яндекса сужением `site:` (РЕШ-79 п. 3).
    source_types = (SourceType.WEB, SourceType.CONTEST)
    #: Бесплатного тарифа нет — без ключа и каталога адаптер не включается
    requires_key = True
    #: Запросы платные и асинхронные; частота ограничена не жёстко
    min_interval_seconds = 0.5
    #: Выдача веб-поиска неструктурирована: заголовок и куски текста страницы.
    #: Организацию, год и авторов из неё достаёт стадия extractor (Э2-5), а не
    #: разбор XML — сам разбор оставляет `org`/`year` пустыми (находка В-2
    #: аудита Э2-Г, инвариант №1, задача З-5): домен и время правки страницы
    #: не доказывают ни организацию, ни год публикации.
    needs_extraction = True

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
        #: Формулировки, не отправленные из-за исчерпанного предела (Ц-1).
        #: Список, а не флаг: важно не «предел кончился», а сколько именно
        #: вопросов источнику не задали — это и есть цена решения предел не
        #: поднимать. Забирается `drain_limit_problems`.
        self._limit_problems: list[str] = []

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        candidates, _ = await self.search_raw(query, limit=limit)
        return candidates

    async def search_raw(self, query: SearchQuery, *, limit: int = 20) -> tuple[list[Candidate], list[dict]]:
        """Кандидаты и сырые записи одного и того же ответа — за один запрос.

        Полный путь поиска живёт здесь, а `search` его только сужает до
        кандидатов: так у сырых записей физически нет способа появиться из
        второго платного вызова — они собираются из того же XML, который тут же
        разбирается в кандидатов (Р-8 плана, критерий Т-6).
        """
        if not (self._api_key and self._folder_id):
            log.warning("yandex_search: нет ключа или идентификатора каталога")
            return [], []
        if self.queries_used >= self._max_queries:
            log.warning(
                "yandex_search: предел %s платных запросов на прогон исчерпан — формулировка «%s» пропущена",
                self._max_queries,
                query.text,
            )
            # Лога прогона снаружи не видно, поэтому пропуск дублируется в
            # проблемы прогона: без записи исчерпанный платный предел в отчёте
            # неотличим от пустой выдачи источника (Ц-1 пост-аудита Э2-Б).
            self._limit_problems.append(
                f"yandex_search: предел {self._max_queries} платных запросов на прогон исчерпан — "
                f"формулировка «{query.text}» источнику НЕ задавалась"
            )
            return [], []

        search_type = self._search_type(query)
        text = _with_site_filter(query, search_type)

        raw = await self._run_search(text, search_type=search_type, limit=limit)
        if raw is None:
            return [], []
        root = self._root(raw)
        if root is None:
            return [], []
        # Тип кандидата — тип формулировки, а не «веб» навсегда: конкурсный
        # запрос обязан вернуть кандидатов `CONTEST`, иначе потолок зрелости
        # (Т-22) и отдельная секция отчёта (Т-24) их не увидят. Стадия
        # extractor размечает свою выдачу тем же `query.source_type`
        # (`extract_candidates`), поэтому разбор адаптера и разбор модели дают
        # одинаковый тип, а не расходятся на откате.
        return (
            self._to_candidates(root, limit=limit, source_type=query.source_type),
            _raw_records(root, limit=limit),
        )

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

    @property
    def limit_problems(self) -> list[str]:
        """Формулировки, пропущенные из-за исчерпанного платного предела.

        Копия списка, а не он сам: доклад о расходе (РЕШ-82) читает накопленное
        за прогон, а забирает записи только `drain_limit_problems`.
        """
        return list(self._limit_problems)

    def drain_limit_problems(self) -> list[str]:
        """Забрать записи о пропусках и обнулить их — по одному разу на проход.

        `SearchOutcome.problems` собирается внутри `gather_candidates`, то есть
        на каждый проход поиска заново. Если бы записи первого прохода приезжали
        во второй ещё раз, недобор в отчёте задвоился бы.
        """
        drained = list(self._limit_problems)
        self._limit_problems.clear()
        return drained

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
        # Считаем домены по `raw_id` (нормализованный адрес — `хост/путь`), а не
        # по `org`: после З-5 `org` у разбора адаптера всегда пуст (домен
        # больше не выдаётся за организацию), но для диагностики доктора счёт
        # уникальных хостов остался нужен и берётся напрямую из ссылки.
        domains = {(c.raw_id or "").split("/", 1)[0] for c in found if c.raw_id}
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
            # Транспортный отказ запуска платного поиска — не тихий пустой
            # список, а SearchError (инвариант №5, РЕШ-45): _safe_search
            # оркестратора поймает его и честно отметит источник недоступным.
            raise SearchError(f"yandex_search: поиск не запустился ({search_type}): {exc}") from exc
        if not operation_id:
            log.warning("yandex_search: ответ без идентификатора операции")
            return None

        return await self._await_operation(operation_id, headers)

    async def _await_operation(self, operation_id: str, headers: dict[str, str]) -> bytes | None:
        """Опрашивать операцию, пока не завершится. Транспортный отказ и
        зависшая операция поднимают SearchError (РЕШ-45)."""
        deadline = asyncio.get_running_loop().time() + self._max_wait
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self._poll_interval)
            try:
                got = await self._client.get(f"{OPERATION_URL}/{operation_id}", headers=headers)
                got.raise_for_status()
                payload = got.json()
            except (httpx.HTTPError, ValueError) as exc:
                # Опрос операции недоступен — тот же транспортный отказ, что
                # и при запуске поиска (инвариант №5, РЕШ-45).
                raise SearchError(f"yandex_search: операция {operation_id} не опрашивается: {exc}") from exc
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

        # Операция зависла и не завершилась за отведённое время — источник не
        # отдал результат, это отказ, а не пустая выдача (инвариант №5, РЕШ-45).
        raise SearchError(f"yandex_search: операция {operation_id} не завершилась за {self._max_wait} с")

    def _root(self, raw: bytes) -> Any | None:
        """Разобрать XML один раз на весь ответ. None — тело нечитаемо.

        Отдельно от сборки кандидатов, потому что из одного и того же дерева
        собираются и кандидаты, и сырые записи для стадии extractor: разбирать
        ответ дважды незачем, а ходить за ним второй раз — тем более.
        """
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            log.warning("yandex_search: XML не разобран: %s", exc)
            return None

        # Яндекс сообщает о своих отказах внутри XML, а не кодом HTTP:
        # исчерпанный лимит, отсутствие прав. Раньше это тихо превращалось в
        # пустой успех — «0» неотличимо от честного «источник ответил и
        # ничего не нашёл», и порог квоты (шесть платных запросов) было
        # нечем измерить. Теперь это SearchError (инвариант №5, РЕШ-45).
        for error in root.iter("error"):
            raise SearchError(f"yandex_search: отказ поиска, код {error.get('code')}: {(error.text or '')[:200]}")
        return root

    def _to_candidates(self, root: Any, *, limit: int, source_type: SourceType = SourceType.WEB) -> list[Candidate]:
        candidates: list[Candidate] = []
        seen: set[str] = set()
        for doc in root.findall(".//doc"):
            candidate = self._to_candidate(doc, source_type)
            if candidate is None or candidate.raw_id in seen:
                continue
            seen.add(candidate.raw_id or "")
            candidates.append(candidate)
            if len(candidates) >= limit:
                break
        return candidates

    def _to_candidate(self, doc: Any, source_type: SourceType = SourceType.WEB) -> Candidate | None:
        url = (doc.findtext("url") or "").strip()
        title = _text_of(doc.find("title"))
        if not url or not title:
            return None

        passages = doc.find("passages")
        snippet = _text_of(passages) if passages is not None else _text_of(doc.find("headline"))

        try:
            return Candidate(
                source_type=source_type,
                title=title,
                url=url,
                # `org`/`year` умышленно не заполняются доменом и `modtime`
                # (находка В-2 аудита Э2-Г, инвариант №1): домен — это
                # предположение о владельце страницы, а не подтверждённое
                # первоисточником имя организации, и `modtime` — время правки
                # страницы, а не год публикации работы. Решать, что из них
                # следует, обязана стадия `extractor` (см. `_raw_records` ниже);
                # разбор адаптера — только запасной путь на её отказ, и в этом
                # запасном пути пустое поле честнее выдумки: отчёт печатает
                # «не найдено» вместо угаданного факта. Домен при этом не
                # теряется — он всё ещё есть в `url` и в `raw_id`, а прежняя
                # группировка карточек по `org` для веба и раньше сводилась к
                # группировке по домену, значит поведение группировки не меняется.
                org=None,
                year=None,
                snippet=snippet[:MAX_SNIPPET],
                raw_id=normalize_url(url),
                provider=self.name,
            )
        except ValidationError as exc:
            log.debug("yandex_search: запись отброшена (%s): %s", url, exc)
            return None


def _raw_records(root: Any, *, limit: int) -> list[dict]:
    """Записи выдачи как есть — вход стадии extractor (Э2-5).

    Те же самые узлы `<doc>`, из которых собираются кандидаты: сырые записи
    ничего не стоят сверх уже оплаченного запроса. Поля переносятся без
    толкования — что из домена и времени правки страницы следует, решает
    стадия, а не адаптер. Запись без адреса пропускается сразу: кандидат без
    ссылки всё равно отбрасывается (правило 1 `prompts/extractor.md`).
    """
    records: list[dict] = []
    for doc in root.findall(".//doc"):
        url = (doc.findtext("url") or "").strip()
        if not url:
            continue
        passages = doc.find("passages")
        records.append(
            {
                "url": url,
                "title": _text_of(doc.find("title")),
                "domain": (doc.findtext("domain") or "").strip(),
                "modtime": (doc.findtext("modtime") or "").strip(),
                "snippet": (_text_of(passages) if passages is not None else _text_of(doc.find("headline")))[
                    :MAX_SNIPPET
                ],
            }
        )
        if len(records) >= limit:
            break
    return records


def has_contest_outcome_marker(text: str) -> bool:
    """Есть ли в тексте формулировки якорь итогов конкурса (РЕШ-117).

    Публичная функция, а не частность `_with_site_filter`: тем же правилом
    гарантирует код `app.pipeline.stages.normalize_contest_queries` (РЕШ-121)
    - опознание конкурсной формулировки "площадки" против "итоги" обязано
    быть ровно одно на обе стороны, иначе они разошлись бы по смыслу.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in CONTEST_OUTCOME_MARKERS)


def _with_site_filter(query: SearchQuery, search_type: str) -> str:
    """Добавить операторы `site:` там, где это осмысленно.

    Сужений два, и они разведены по типу формулировки, а не свалены в одно
    условие:

    * `contest` без якоря итогов (`has_contest_outcome_marker`) — по списку
      конкурсных площадок (`CONTEST_SITES`). Признак берётся из типа запроса,
      а не из слов в тексте: конкурсная формулировка без площадок не
      отличается от обычной веб-строки. `contest` С якорем итогов уходит БЕЗ
      сужения (РЕШ-107, РЕШ-117): новость о победителе или разбор итогов
      чаще лежит на сайте вуза или в отраслевом издании, которых в
      CONTEST_SITES никогда не будет, а сужение вернуло бы те же рекламные
      страницы площадок, из-за которых конкурсная очередь дала 0 карточек
      при 20 кандидатах на прогон;
    * `web` — по реестрам грантовых фондов (`GRANT_SITES`), и только в
      российском контуре и только при грантовом намерении в тексте
      (`GRANT_INTENT_MARKERS`, РЕШ-41). Конкурсные площадки сюда не попадают:
      слово «конкурс» есть и среди грантовых маркеров, и подмешивание площадок
      увело бы грантовый запрос из реестров РНФ и Фонда содействия.

    Для всего остального ограничение по сайтам убило бы смысл открытого веба.
    """
    if query.source_type is SourceType.CONTEST:
        if has_contest_outcome_marker(query.text):
            return query.text
        return _narrowed(query.text, CONTEST_SITES)
    if search_type != "SEARCH_TYPE_RU" or query.source_type is not SourceType.WEB:
        return query.text
    if not any(marker in query.text.lower() for marker in GRANT_INTENT_MARKERS):
        return query.text
    return _narrowed(query.text, GRANT_SITES)


def _narrowed(text: str, sites: tuple[str, ...]) -> str:
    """Формулировка, суженная до перечисленных площадок оператором `site:`."""
    return f"{text} ({' | '.join(f'site:{site}' for site in sites)})"


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
