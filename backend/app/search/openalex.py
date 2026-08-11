"""OpenAlex — ЗАГОТОВКА. Только обогащение по DOI, никогда не поиск.

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
    цитируемость (cited_by_count), концепты/темы, open access статус.
    Полнотекстовый поиск через OpenAlex не делать НИКОГДА — это платный вызов
    search, который дублирует бесплатный Crossref.

АЛЬТЕРНАТИВА БЕЗ ДЕНЕГ
    Полный снапшот выкладывается в S3 и качается бесплатно, без ключа:
        aws s3 sync --no-sign-request s3://openalex/ ./openalex-snapshot/
    Это ~400 ГБ сжатого JSONL. Имеет смысл, если понадобится массовая
    аналитика по организациям РФ, а не точечное обогащение.

НАСТРОЙКИ
    settings.openalex_api_key — обязателен, без него адаптер не включается
    (requires_key = True, см. build_adapters в app/search/__init__.py).

ЧТО СДЕЛАТЬ (задача Э2-1)
    1. В конструкторе запомнить api_key; заголовок авторизации —
       {"Authorization": f"Bearer {api_key}"} (проверить актуальную схему в
       документации, OpenAlex обещал поддержать и параметр api_key=).
    2. Реализовать enrich_by_doi(dois): чанками по 50 DOI собирать
       filter=doi:doi1|doi2|... — это list-вызов, он дешёвый; либо, если нужен
       гарантированно бесплатный путь, дёргать /works/doi:{doi} по одному.
    3. Из ответа брать: authorships[].institutions[].display_name и .ror,
       cited_by_count, publication_year, best_oa_location.pdf_url, topics[].
    4. Возвращать не Candidate, а патч к уже найденному кандидату (org,
       authors, year) — обогащение не создаёт новых кандидатов.
    5. search() оставить запрещённым: явно бросать NotImplementedError с
       пояснением, что полнотекстовый поиск здесь платный и не нужен.
    6. Учёт денег: каждый list-вызов = $0.10/1000, писать в лог счётчик
       вызовов, чтобы стоимость попадала в Report.cost_rub.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchAdapter

API_URL = "https://api.openalex.org/works"


class OpenAlexAdapter(SearchAdapter):
    """Обогащение метаданных по DOI. Поиск через этот источник запрещён."""

    name = "openalex"
    source_types = (SourceType.PUBLICATION,)
    #: С февраля 2026 ключ обязателен
    requires_key = True
    #: Флаг для реестра: адаптер в build_adapters не попадает
    implemented = False

    def __init__(self, client: httpx.AsyncClient, *, api_key: str | None = None) -> None:
        super().__init__(client)
        self._api_key = api_key or None

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        raise NotImplementedError(
            "openalex: полнотекстовый поиск платный ($1/1000) и дублирует Crossref — "
            "источник используется только для обогащения по DOI, задача Э2-1"
        )

    async def enrich_by_doi(self, dois: list[str]) -> dict[str, dict[str, Any]]:
        """Добрать по списку DOI организации, цитируемость и OA-ссылки.

        Возвращает отображение DOI (в нижнем регистре) → патч метаданных.
        Lookup по DOI бесплатный и безлимитный — это единственный разрешённый
        режим работы с OpenAlex.
        """
        raise NotImplementedError("openalex: обогащение по DOI не реализовано, задача Э2-1")

    async def health(self) -> tuple[bool, str]:
        return False, "не реализовано, задача Э2-1"
