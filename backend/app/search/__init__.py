"""Реестр поисковых источников.

Пайплайн не знает про Crossref и КиберЛенинку — он просит список адаптеров и
работает с ними через один контракт SearchAdapter. Здесь решается только одно:
какие источники вообще можно включить в этом запуске.

Правила включения (build_adapters):

* адаптер-заготовка (implemented = False) не включается никогда — иначе его
  NoImplementedError уронил бы пайплайн;
* адаптер без ключа (requires_key = False) включается всегда;
* адаптер с ключом включается, только если ВСЕ его настройки заполнены
  (Яндексу, например, нужны и ключ, и идентификатор каталога);
* необязательные настройки (ключ Semantic Scholar, каталог кэша КиберЛенинки,
  фабрика сессий витрины) просто передаются в конструктор, если заданы.

Порядок в списке значимый: витрина партнёров идёт первой — это приоритетный
слой с уже подтверждёнными контактами разработчиков.

Отдельный режим `include_stubs=True` — для `scout doctor` (РЕШ-52.1): там нужен
не рабочий набор, а полная картина. В этом режиме из списка не выбрасывается
никто: и заготовка, и источник без ключа возвращаются с пометкой, почему они не
работают (`adapter_status`). Раньше доктор молчал о заготовках вовсе, и
проверить статус источника, которого нет в списке, было нечем.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import httpx

from app.search.arxiv import ArxivAdapter
from app.search.base import SearchAdapter, SearchError
from app.search.crossref import CrossrefAdapter
from app.search.cyberleninka import CyberLeninkaAdapter
from app.search.openalex import OpenAlexAdapter, normalize_doi
from app.search.patents_view import PatentsViewAdapter
from app.search.rospatent import RospatentAdapter
from app.search.semanticscholar import SemanticScholarAdapter
from app.search.showcase import ShowcaseAdapter
from app.search.yandex_search import YandexSearchAdapter

log = logging.getLogger(__name__)

#: Все известные источники. Порядок = приоритет в отчёте.
ADAPTERS: dict[str, type[SearchAdapter]] = {
    "showcase": ShowcaseAdapter,
    "crossref": CrossrefAdapter,
    "cyberleninka": CyberLeninkaAdapter,
    "arxiv": ArxivAdapter,
    "semanticscholar": SemanticScholarAdapter,
    "rospatent": RospatentAdapter,
    "openalex": OpenAlexAdapter,
    "patents_view": PatentsViewAdapter,
    "yandex_search": YandexSearchAdapter,
}

#: Настройки, без которых источник с requires_key = True не включается
REQUIRED_SETTINGS: dict[str, tuple[str, ...]] = {
    # OpenAlex здесь больше нет: поиск у него бесплатен и ключа не требует
    # (Т-15, РЕШ-71). Ключ нужен только обогащению по DOI, и его отсутствие
    # проверяет сам адаптер — выключать из-за этого поиск нельзя.
    #
    # Поисковая платформа Роспатента работает только по ключу; режима «без
    # ключа» у адаптера нет, поэтому без настройки источник не включается
    # вовсе (Т-14, РЕШ-69).
    "rospatent": ("rospatent_api_key",),
    "patents_view": ("patentsview_api_key",),
    # Яндексу нужны оба значения: ключ сервисного аккаунта и folder id
    "yandex_search": ("yandex_search_api_key", "yandex_catalog_id"),
}

#: Что подставлять в конструктор: имя аргумента → варианты имён настроек
CONSTRUCTOR_SETTINGS: dict[str, dict[str, tuple[str, ...]]] = {
    "showcase": {"session_factory": ("showcase_session_factory", "session_factory")},
    # Набора OAI у КиберЛенинки больше нет: с Т-16 (РЕШ-68) она ищет через
    # собственный поисковый API, у которого наборов не существует. Настройка
    # набора убрана из config.py вместе с этим путём (Т-25).
    "cyberleninka": {
        "cache_dir": ("cyberleninka_cache_dir",),
    },
    "semanticscholar": {
        "api_key": ("semantic_scholar_api_key", "semanticscholar_api_key"),
    },
    "rospatent": {
        "api_key": ("rospatent_api_key",),
        # Контакт владельца в User-Agent. Отдельной настройки под Роспатент
        # нет — почта в проекте одна, и заведена она как `crossref_mailto`.
        "contact_email": ("rospatent_mailto", "crossref_mailto"),
    },
    "openalex": {"api_key": ("openalex_api_key",)},
    "patents_view": {"api_key": ("patentsview_api_key",)},
    "yandex_search": {
        "api_key": ("yandex_search_api_key",),
        "folder_id": ("yandex_catalog_id", "yandex_folder_id"),
    },
}


#: Короткий статус источника-заготовки в таблице доктора
STATUS_STUB = "не реализовано"
#: Короткий статус источника, выключенного из-за незаполненных настроек
STATUS_NO_KEY = "нет ключа"
#: Имя атрибута экземпляра, куда build_adapters кладёт пару
#: (короткий статус, пояснение). Живёт на экземпляре, а не в `SearchAdapter`:
#: базовый контракт источника к сборке реестра отношения не имеет.
_STATUS_ATTR = "disabled_status"


def adapter_status(adapter: SearchAdapter) -> tuple[str, str] | None:
    """Почему источник в списке есть, но работать не будет.

    None — источник рабочий. Иначе пара «короткий статус, пояснение»: её
    печатает `scout doctor`. Значений ключей в пояснении нет и быть не может —
    только имена переменных окружения (инвариант №3).
    """
    return getattr(adapter, _STATUS_ATTR, None)


def build_adapters(
    client: httpx.AsyncClient,
    settings: Any = None,
    *,
    include_stubs: bool = False,
) -> list[SearchAdapter]:
    """Собрать адаптеры, которые реально могут работать в этом запуске.

    settings — любой объект с атрибутами-настройками (pydantic Settings,
    dataclass, SimpleNamespace) или словарь. None означает «ключей нет»,
    тогда останутся только бесплатные источники.

    `include_stubs=True` меняет смысл списка: возвращаются ВСЕ источники из
    `ADAPTERS`, а нерабочие получают статус (`adapter_status`) вместо
    исключения из списка. Этот режим — для доктора; пайплайн зовёт функцию без
    него и получает только то, что действительно работает.

    Отрасли прогона здесь больше нет (Т-25): единственным её потребителем была
    КиберЛенинка, выбиравшая по отрасли набор OAI, а с Т-16 (РЕШ-68) наборов у
    неё не осталось — путь передачи вёл в никуда. Понадобится источнику отрасль
    снова — аргумент возвращается вместе с тем, кто его читает.
    """
    adapters: list[SearchAdapter] = []
    for name, cls in ADAPTERS.items():
        stub = not getattr(cls, "implemented", True)
        if stub and not include_stubs:
            log.debug("источник %s — заготовка, пропущен", name)
            continue
        missing = [key for key in REQUIRED_SETTINGS.get(name, ()) if not _setting(settings, key)]
        no_key = bool(cls.requires_key and missing)
        if no_key and not include_stubs:
            log.info("источник %s выключен: нет настроек %s", name, ", ".join(missing))
            continue
        try:
            adapter = _instantiate(name, cls, client, settings)
        except Exception as exc:
            log.warning("источник %s не собрался: %s", name, exc)
            continue
        status: tuple[str, str] | None = None
        if stub:
            status = (STATUS_STUB, "заготовка: источник ещё не подключён")
        elif no_key:
            status = (STATUS_NO_KEY, "не заполнено: " + ", ".join(key.upper() for key in missing))
        setattr(adapter, _STATUS_ATTR, status)
        adapters.append(adapter)
    working = [a.name for a in adapters if adapter_status(a) is None]
    log.info("активные источники: %s", ", ".join(working) or "нет")
    return adapters


def free_adapters() -> dict[str, type[SearchAdapter]]:
    """Реализованные источники, которым не нужен ключ — на них строятся тесты."""
    return {name: cls for name, cls in ADAPTERS.items() if getattr(cls, "implemented", True) and not cls.requires_key}


def _instantiate(
    name: str,
    cls: type[SearchAdapter],
    client: httpx.AsyncClient,
    settings: Any,
) -> SearchAdapter:
    """Передать в конструктор только те аргументы, которые он принимает."""
    accepted = inspect.signature(cls.__init__).parameters
    kwargs: dict[str, Any] = {}
    for argument, keys in CONSTRUCTOR_SETTINGS.get(name, {}).items():
        if argument not in accepted:
            continue
        for key in keys:
            if (value := _setting(settings, key)) is not None:
                kwargs[argument] = value
                break
    return cls(client, **kwargs)


def _setting(settings: Any, key: str) -> Any:
    """Достать настройку из объекта или словаря. Пустая строка = не задана.

    В app/config.py ключи объявлены как pydantic SecretStr, поэтому обёртку
    надо развернуть: иначе в заголовок ушёл бы объект SecretStr, а пустой
    SecretStr("") сошёл бы за заполненный ключ и включил платный источник.
    """
    if settings is None:
        return None
    value = settings.get(key) if isinstance(settings, dict) else getattr(settings, key, None)
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    if isinstance(value, str) and not value.strip():
        return None
    return value


__all__ = [
    "ADAPTERS",
    "REQUIRED_SETTINGS",
    "STATUS_NO_KEY",
    "STATUS_STUB",
    "ArxivAdapter",
    "CrossrefAdapter",
    "CyberLeninkaAdapter",
    "OpenAlexAdapter",
    "PatentsViewAdapter",
    "RospatentAdapter",
    "SearchAdapter",
    "SearchError",
    "SemanticScholarAdapter",
    "ShowcaseAdapter",
    "YandexSearchAdapter",
    "adapter_status",
    "build_adapters",
    "free_adapters",
    "normalize_doi",
]
