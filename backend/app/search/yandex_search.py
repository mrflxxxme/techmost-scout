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

import httpx

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.analog_sources import plan_analog_queries
from app.search.base import SearchAdapter, SearchError
from app.search.yandex_parse import (
    CONTEST_OUTCOME_MARKERS,
    CONTEST_SITES,
    GRANT_SITES,
    MAX_SNIPPET,
    contour_label_for,
    extract_root,
    has_contest_outcome_marker,
    raw_records,
    search_type_for,
    to_candidates,
)
from app.search.yandex_parse import (
    with_site_filter as _with_site_filter,
)

log = logging.getLogger(__name__)

__all__ = [
    "CONTEST_OUTCOME_MARKERS",
    "CONTEST_SITES",
    "GRANT_SITES",
    "MAX_SNIPPET",
    "OPERATION_URL",
    "SEARCH_ASYNC_URL",
    "YandexSearchAdapter",
    "_with_site_filter",
    "has_contest_outcome_marker",
]

SEARCH_ASYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations"

#: Яндекс отдаёт не больше сотни на страницу; нам столько и не нужно
MAX_PER_PAGE = 100


class YandexSearchAdapter(SearchAdapter):
    """Поиск по открытому вебу: российский и мировой контуры одним ключом."""

    name = "yandex_search"
    #: `CONTEST` здесь не украшение: оркестратор отдаёт адаптеру только те
    #: формулировки, чей тип перечислен в `source_types`, и без этой строки
    #: конкурсный запрос молча не доходил бы ни до одного источника
    #: (урок `JOURNAL.md:177`). Своего адаптера под конкурсы нет — площадки
    #: читаются через тот же контур Яндекса сужением `site:` (РЕШ-79 п. 3).
    #: `GRANT` добавлен РЕШ-173: грантовая формулировка планировщика больше не
    #: маскируется под `web` — она доходит до адаптера собственным типом, и
    #: кандидаты из неё размечаются им же (см. `search_raw`).
    #: `ANALOG` добавлен Э6-2а: без него формулировка `analog` из плана при виде
    #: решения «Рынок» не доходила бы ни до одного источника (тип недостижим).
    source_types = (SourceType.WEB, SourceType.CONTEST, SourceType.GRANT, SourceType.ANALOG)
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
        if query.source_type is SourceType.ANALOG:
            return await self._search_analog(query, limit=limit)
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

        search_type = search_type_for(query)
        text = _with_site_filter(query, search_type)

        raw = await self._run_search(text, search_type=search_type, limit=limit)
        if raw is None:
            return [], []
        root = extract_root(raw)
        if root is None:
            return [], []
        # Тип кандидата — тип формулировки, а не «веб» навсегда: конкурсный
        # запрос обязан вернуть кандидатов `CONTEST`, иначе потолок зрелости
        # (Т-22) и отдельная секция отчёта (Т-24) их не увидят. Стадия
        # extractor размечает свою выдачу тем же `query.source_type`
        # (`extract_candidates`), поэтому разбор адаптера и разбор модели дают
        # одинаковый тип, а не расходятся на откате.
        return (
            to_candidates(root, limit=limit, source_type=query.source_type, provider=self.name),
            raw_records(root, limit=limit),
        )

    async def _search_analog(self, query: SearchQuery, *, limit: int) -> tuple[list[Candidate], list[dict]]:
        """Формулировка `analog` по белому списку доменов — пачками (Э6-2в).

        Все домены в один запрос не влезают (предел длины — `ANALOG_QUERY_MAX_CHARS`),
        поэтому каждая пачка уходит отдельным платным запросом. Пачки, не
        поместившиеся в предел запросов на прогон, и домены, которым не нашлось
        места в формулировке, называются в `limit_problems` поимённо: пропуск
        виден в отчёте и не путается с пустой выдачей (инвариант №5).

        Отказ источника на любой пачке поднимается наружу как `SearchError`: уже
        полученное по ранним пачкам при этом теряется, зато отказ не прячется.
        """
        plan = plan_analog_queries(query.text, lang=query.lang)
        if plan.unfit:
            self._limit_problems.append(
                f"yandex_search: формулировка «{query.text}» слишком длинна для оператора site: — "
                f"домены не опрошены: {', '.join(plan.unfit)}"
            )
        search_type = search_type_for(query)
        candidates: list[Candidate] = []
        records: list[dict] = []
        seen: set[str] = set()
        for index, text in enumerate(plan.texts):
            if self.queries_used >= self._max_queries:
                skipped = [domain for batch in plan.batches[index:] for domain in batch]
                log.warning("yandex_search: предел %s платных запросов исчерпан — аналоги пропущены", self._max_queries)
                self._limit_problems.append(
                    f"yandex_search: предел {self._max_queries} платных запросов на прогон исчерпан — "
                    f"формулировка «{query.text}» источнику НЕ задавалась для доменов: {', '.join(skipped)}"
                )
                break
            raw = await self._run_search(text, search_type=search_type, limit=limit)
            root = extract_root(raw) if raw is not None else None
            if root is None:
                continue
            for candidate in to_candidates(root, limit=limit, source_type=query.source_type, provider=self.name):
                if candidate.raw_id not in seen:
                    seen.add(candidate.raw_id or "")
                    candidates.append(candidate)
            records.extend(raw_records(root, limit=limit))
        return candidates, records

    def contour_label(self, query: SearchQuery) -> str:
        """Метка контура для оркестратора: `yandex-ru` или `yandex-com`.

        Берёт тот же контур, что реально уйдёт в `search()` — метка и правило
        выбора не могут разойтись, потому что вычисляются одной функцией.
        """
        return contour_label_for(search_type_for(query))

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
