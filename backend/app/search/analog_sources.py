"""Белый список доменов для поиска аналогов и разбиение его на пачки (Э6-2в).

Формулировка `analog` ищет в открытом вебе не где угодно, а только на сайтах
организаций, которые публикуют отраслевые обзоры и разбор внедрений: банки,
фонды, консультанты и каталоги стартапов. Список лежит в
`reference/analog_sources.yaml` — домен добавляется правкой файла, без кода.

Оператор `site:` съедает длину запроса, а у Яндекса она ограничена, поэтому все
домены в одну формулировку не влезают. Модуль делит их на пачки: каждая пачка —
отдельная формулировка и отдельный платный запрос (учёт предела — в адаптере,
`search/yandex_search.py`). Что не влезло — возвращается названием, а не теряется
молча (инвариант №5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_REFERENCE_PATH = Path(__file__).resolve().parent.parent / "reference" / "analog_sources.yaml"

#: Предел длины `queryText` в запросе к Yandex Search API — 400 символов.
#: Источник: описание поля `query_text` (`(length) = "<=400"`) в схеме сервиса
#: https://github.com/yandex-cloud/cloudapi/blob/master/yandex/cloud/searchapi/v2/search_query.proto
#: Страницы справки https://yandex.cloud/ru/docs/search-api/api-ref/WebSearch/search
#: отдают роботу капчу, поэтому сверка сделана по этому файлу. Считается в
#: символах всей формулировки вместе с оператором `site:`.
ANALOG_QUERY_MAX_CHARS = 400

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def narrow_text(text: str, sites: tuple[str, ...]) -> str:
    """Формулировка, суженная до перечисленных сайтов оператором `site:`."""
    return f"{text} ({' | '.join(f'site:{site}' for site in sites)})"


@dataclass(frozen=True)
class AnalogSource:
    """Одна запись белого списка: домен и вид организации."""

    domain: str
    kind: str


@dataclass(frozen=True)
class AnalogQueryPlan:
    """Формулировка `analog`, разложенная по пачкам доменов.

    `texts[i]` — готовая формулировка для пачки `batches[i]`. `unfit` — домены,
    которые не влезли ни в одну пачку (сама формулировка слишком длинна).
    """

    texts: tuple[str, ...]
    batches: tuple[tuple[str, ...], ...]
    unfit: tuple[str, ...]


def _check_domain(value: object, section: str) -> str:
    """Домен — только имя узла: строчными, без схемы, пути и пробелов."""
    if not isinstance(value, str) or not _DOMAIN_RE.fullmatch(value):
        raise ValueError(
            f"Белый список аналогов: в секции «{section}» домен {value!r} не годится — "
            "нужно имя узла строчными буквами, без схемы, пути и пробелов"
        )
    return value


def _entries(raw: Any, section: str) -> tuple[AnalogSource, ...]:
    """Записи секции `{domain, kind}`; пустая секция и нестрока — `ValueError`."""
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"Белый список аналогов: секция «{section}» должна быть непустым списком")
    entries: list[AnalogSource] = []
    for item in raw:
        if not isinstance(item, dict) or "domain" not in item:
            raise ValueError(f"Белый список аналогов: запись без поля domain в секции «{section}»")
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(f"Белый список аналогов: у домена {item['domain']!r} не указан вид (kind)")
        entries.append(AnalogSource(domain=_check_domain(item["domain"], section), kind=kind))
    return tuple(entries)


@lru_cache(maxsize=1)
def load_analog_sources() -> dict[str, Any]:
    """Прочитать и проверить список; результат кэшируется на процесс.

    Возвращает `checked_at` и кортежи записей `reports` и `catalogs`. Чужие
    секции файла (например, реестры РФ) игнорируются. Домен со схемой, путём,
    пробелом, прописными буквами или повтор (в том числе между секциями) —
    `ValueError`: тихо поправленный домен хуже остановленной загрузки.
    """
    data = yaml.safe_load(_REFERENCE_PATH.read_text(encoding="utf-8"))
    return _validate(data)


def _validate(data: Any) -> dict[str, Any]:
    """Проверка прочитанного YAML; отдельно от чтения файла — чтобы тестироваться без него."""
    if not isinstance(data, dict):
        raise ValueError("Белый список аналогов: корень файла должен быть словарём")
    checked_at = data.get("checked_at")
    if not isinstance(checked_at, str) or not checked_at:
        raise ValueError("Белый список аналогов: checked_at обязателен и пишется строкой в кавычках")
    reports = _entries(data.get("reports"), "reports")
    catalogs = _entries(data.get("catalogs"), "catalogs")
    domains = [entry.domain for entry in (*catalogs, *reports)]
    if len(set(domains)) != len(domains):
        repeated = sorted({d for d in domains if domains.count(d) > 1})
        raise ValueError(f"Белый список аналогов: домен повторяется: {', '.join(repeated)}")
    return {"checked_at": checked_at, "reports": reports, "catalogs": catalogs}


def analog_domains() -> tuple[str, ...]:
    """Все домены списка в порядке опроса: каталоги, затем отчёты.

    Каталоги стартапов идут первыми: при нехватке платных запросов в предел
    влезают они, а хвост отчётов называется в `limit_problems`.
    """
    loaded = load_analog_sources()
    return tuple(entry.domain for entry in (*loaded["catalogs"], *loaded["reports"]))


def plan_analog_queries(
    text: str, domains: tuple[str, ...] | None = None, *, max_chars: int = ANALOG_QUERY_MAX_CHARS
) -> AnalogQueryPlan:
    """Разложить домены по пачкам так, чтобы каждая формулировка была ≤ `max_chars`.

    Жадно, в порядке списка. Домен, который не помещается даже в одиночку
    (формулировка слишком длинна), попадает в `unfit` и в пачки не входит.
    """
    pool = analog_domains() if domains is None else domains
    batches: list[list[str]] = []
    unfit: list[str] = []
    for domain in pool:
        if len(narrow_text(text, (domain,))) > max_chars:
            unfit.append(domain)
            continue
        if batches and len(narrow_text(text, (*batches[-1], domain))) <= max_chars:
            batches[-1].append(domain)
        else:
            batches.append([domain])
    frozen = tuple(tuple(batch) for batch in batches)
    return AnalogQueryPlan(
        texts=tuple(narrow_text(text, batch) for batch in frozen),
        batches=frozen,
        unfit=tuple(unfit),
    )
