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
* необязательные настройки (ключ Semantic Scholar, набор OAI КиберЛенинки,
  фабрика сессий витрины) просто передаются в конструктор, если заданы.

Порядок в списке значимый: витрина партнёров идёт первой — это приоритетный
слой с уже подтверждёнными контактами разработчиков.
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
from app.search.openalex import OpenAlexAdapter
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
    "openalex": ("openalex_api_key",),
    "patents_view": ("patentsview_api_key",),
    # Яндексу нужны оба значения: ключ сервисного аккаунта и folder id
    "yandex_search": ("yandex_search_api_key", "yandex_catalog_id"),
}

#: Что подставлять в конструктор: имя аргумента → варианты имён настроек
CONSTRUCTOR_SETTINGS: dict[str, dict[str, tuple[str, ...]]] = {
    "showcase": {"session_factory": ("showcase_session_factory", "session_factory")},
    "cyberleninka": {
        "oai_set": ("cyberleninka_oai_set",),
        "cache_dir": ("cyberleninka_cache_dir",),
    },
    "semanticscholar": {
        "api_key": ("semantic_scholar_api_key", "semanticscholar_api_key"),
    },
    "rospatent": {"api_key": ("rospatent_api_key",)},
    "openalex": {"api_key": ("openalex_api_key",)},
    "patents_view": {"api_key": ("patentsview_api_key",)},
    "yandex_search": {
        "api_key": ("yandex_search_api_key",),
        "folder_id": ("yandex_catalog_id", "yandex_folder_id"),
    },
}


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
    """
    adapters: list[SearchAdapter] = []
    for name, cls in ADAPTERS.items():
        if not include_stubs and not getattr(cls, "implemented", True):
            log.debug("источник %s — заготовка, пропущен", name)
            continue
        missing = [key for key in REQUIRED_SETTINGS.get(name, ()) if not _setting(settings, key)]
        if cls.requires_key and missing:
            log.info("источник %s выключен: нет настроек %s", name, ", ".join(missing))
            continue
        try:
            adapters.append(_instantiate(name, cls, client, settings))
        except Exception as exc:
            log.warning("источник %s не собрался: %s", name, exc)
    log.info("активные источники: %s", ", ".join(a.name for a in adapters) or "нет")
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
    "build_adapters",
    "free_adapters",
]
