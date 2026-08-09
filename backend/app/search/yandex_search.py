"""Яндекс Поиск — ЗАГОТОВКА. Открытый веб там, где API не существует.

ЗАЧЕМ ЭТОТ ИСТОЧНИК
    Значительная часть нужного нам живёт только на сайтах и не имеет API:
    страницы лабораторий и кафедр, центры трансфера технологий (ЦТТ), реестры
    поддержанных проектов РНФ и ФСИ (Фонд содействия инновациям), новости о
    пилотных внедрениях, отчёты по НИОКР. Без веб-поиска эти следы не найти.

ИСТОЧНИК И ЦЕНА
    Yandex Search API (Yandex Cloud), REST/gRPC:
        https://yandex.cloud/ru/docs/search-api/
        POST https://searchapi.api.cloud.yandex.net/v2/web/searchAsync
        (есть и синхронный /v2/web/search с более жёстким лимитом)
    Платный, БЕСПЛАТНОГО ТАРИФА НЕТ. Порядок цены — 50-200 ₽ за 1000 запросов,
    зависит от типа поиска и объёма; тариф уточнять в консоли Yandex Cloud.
    Значит: каждый вызов стоит денег и обязан попадать в Report.cost_rub.

НАСТРОЙКИ (нужны ОБА, иначе адаптер не включается)
    settings.yandex_search_api_key — API-ключ сервисного аккаунта
    settings.yandex_catalog_id     — идентификатор каталога (folder id)
    requires_key = True.

ПРО АЛЬТЕРНАТИВЫ
    Brave Search API как бесплатная замена БОЛЬШЕ НЕ ГОДИТСЯ: бесплатный тариф
    закрыт в феврале 2026, для регистрации требуют привязку карты. То есть
    «бесплатного веб-поиска через API» на этапе проектирования не существует;
    либо платим Яндексу, либо отказываемся от слоя открытого веба.
    Отдельно держать в голове: Яндекс работает из РФ и лучше индексирует
    русскоязычные сайты вузов — для нашей задачи это плюс перед Google CSE.

ЧТО СДЕЛАТЬ (этап 2)
    1. Конструктор: api_key + folder_id, оба обязательны.
    2. Запрос: POST searchAsync с телом
       {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": <текст>},
        "folderId": <folder_id>, "responseFormat": "FORMAT_XML",
        "groupSpec": {"groupsOnPage": <limit>}}
       Заголовок: {"Authorization": f"Api-Key {api_key}"}.
    3. searchAsync возвращает операцию; результат — base64 XML, его надо
       забрать по id операции (опрос с паузой) и раскодировать.
       Разбирать xml.etree: /yandexsearch/response/results/grouping/group/doc,
       поля url, title, headline, passages.
    4. Собирать Candidate: source_type = SourceType.WEB, provider =
       "yandex_search", snippet — из passages, url — из doc/url,
       raw_id = None (у веб-страницы нет стабильного идентификатора).
    5. Сузить выдачу через операторы Яндекса: site:, host:, mime:pdf —
       например добавлять к запросу «site:ru» и минус-слова по мусорным
       агрегаторам; список держать в data/, не в коде.
    6. Считать деньги: инкрементировать счётчик запросов и стоимость, чтобы
       оператор видел цену перед запуском (веб-слой — самый дорогой).
    7. Троттлинг: min_interval_seconds по лимиту тарифа, начать с 1.0.
"""

from __future__ import annotations

import httpx

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter

SEARCH_ASYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"


class YandexSearchAdapter(SearchAdapter):
    """Поиск по открытому вебу: сайты институтов, ЦТТ, страницы грантов."""

    name = "yandex_search"
    source_types = (SourceType.WEB,)
    #: Бесплатного тарифа нет — без ключа и каталога адаптер не включается
    requires_key = True
    #: Флаг для реестра: адаптер в build_adapters не попадает
    implemented = False

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str | None = None,
        folder_id: str | None = None,
    ) -> None:
        super().__init__(client)
        self._api_key = api_key or None
        self._folder_id = folder_id or None

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        raise NotImplementedError("yandex_search: не реализовано, этап 2")

    async def health(self) -> tuple[bool, str]:
        return False, "не реализовано, этап 2"
