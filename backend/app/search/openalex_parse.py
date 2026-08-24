"""OpenAlex: разбор ответа источника — маппинг полей и инвертированный индекс.

Вынесено из `search/openalex.py` (Д-5 волны Э2-З) отдельным модулем: адаптер
занимается сетью и троттлингом, этот модуль — только чтением уже полученного
JSON. Ни одна функция здесь не делает сетевых вызовов и не хранит состояния,
поэтому весь модуль тестируется на голых словарях без respx.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import HttpUrl, ValidationError

from app.schemas import Candidate, SourceType

log = logging.getLogger(__name__)

#: Сколько авторов забираем в карточку — столько же, сколько у Crossref
MAX_AUTHORS = 12
#: Аннотация в OpenAlex лежит инвертированным индексом; собранный текст режем
#: по той же границе, что и Crossref, — вход скоринга не резиновый.
MAX_SNIPPET = 700
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:")
_WS = re.compile(r"\s+")


def normalize_doi(value: Any) -> str:
    """DOI в том виде, в каком он уходит в lookup: без схемы, в нижнем регистре.

    Пустая строка означает «это не DOI»: у патента или веб-страницы в `raw_id`
    лежит номер или адрес, и обогащать по нему нечего.
    """
    if not isinstance(value, str):
        return ""
    doi = value.strip().lower()
    for prefix in _DOI_PREFIXES:
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
            break
    doi = doi.strip()
    return doi if doi.startswith("10.") else ""


def work_to_candidate(work: dict[str, Any], *, provider: str) -> Candidate | None:
    """Одна работа выдачи поиска → кандидат. None — запись без названия или ссылки.

    Ссылка обязательна: кандидат без неё нечем подтвердить в карточке. Порядок
    предпочтения — DOI, посадочная страница издателя, постоянный адрес самой
    работы в OpenAlex.
    """
    title = _text(work.get("display_name"))
    if not title:
        return None

    doi = normalize_doi(work.get("doi"))
    url = f"https://doi.org/{doi}" if doi else _landing_page(work) or _text(work.get("id"))
    if not url:
        return None

    journal = _source_name(work.get("primary_location"))
    abstract = _abstract_from_inverted_index(work.get("abstract_inverted_index"))
    snippet = " — ".join(part for part in (journal, abstract) if part)[:MAX_SNIPPET]

    try:
        candidate = Candidate(
            source_type=SourceType.PUBLICATION,
            title=title,
            url=url,
            org=_first_institution(work.get("authorships")) or journal or None,
            authors=_authors(work.get("authorships")),
            year=_year(work.get("publication_year")),
            snippet=snippet,
            raw_id=doi or None,
            provider=provider,
        )
    except ValidationError as exc:
        log.debug("openalex: запись отброшена (%s): %s", url, exc)
        return None

    # Негодная ссылка на полный текст отваливается в одиночку: терять из-за
    # неё название, организацию и реферат — то же самое, что П-2 аудита
    # запретил обогащению.
    pdf_url = _pdf_url(work)
    if pdf_url:
        try:
            return candidate.model_copy(update={"pdf_url": HttpUrl(pdf_url)})
        except ValidationError:
            log.info("openalex: поле pdf_url отброшено у «%s»: негодная ссылка", title)
    return candidate


def payload_to_patch(payload: dict[str, Any]) -> dict[str, Any]:
    """Патч к найденному кандидату: только те поля, которые OpenAlex реально отдал.

    Пустых значений в патче не бывает: «организация не указана» здесь означает
    отсутствие ключа, а не строку «не указана» — иначе обогащение затирало бы
    честное «неизвестно» выдуманным содержимым (инвариант №1).
    """
    patch: dict[str, Any] = {}

    org = _first_institution(payload.get("authorships"))
    if org:
        patch["org"] = org

    snippet = _abstract_from_inverted_index(payload.get("abstract_inverted_index"))
    if snippet:
        patch["snippet"] = snippet

    location = payload.get("best_oa_location")
    if isinstance(location, dict):
        pdf_url = str(location.get("pdf_url") or "").strip()
        if pdf_url:
            patch["pdf_url"] = pdf_url

    cited = payload.get("cited_by_count")
    if isinstance(cited, int) and not isinstance(cited, bool) and cited >= 0:
        patch["cited_by_count"] = cited

    return patch


def _first_institution(authorships: Any) -> str:
    """Организация первого автора, у которого она вообще проставлена.

    Аффилиации OpenAlex нормализованы (у каждой есть ROR), поэтому они точнее
    сырых строк Crossref — но подставляются только в пустое поле кандидата.
    """
    if not isinstance(authorships, list):
        return ""
    for authorship in authorships:
        if not isinstance(authorship, dict):
            continue
        for institution in authorship.get("institutions") or []:
            if not isinstance(institution, dict):
                continue
            name = str(institution.get("display_name") or "").strip()
            if name:
                return name
    return ""


def _text(value: Any) -> str:
    """Строка источника в пригодном виде: без переносов и лишних пробелов."""
    return _WS.sub(" ", value).strip() if isinstance(value, str) else ""


def _landing_page(work: dict[str, Any]) -> str:
    """Посадочная страница издателя — запасная ссылка для работы без DOI."""
    location = work.get("primary_location")
    if not isinstance(location, dict):
        return ""
    return _text(location.get("landing_page_url"))


def _source_name(location: Any) -> str:
    """Название журнала или репозитория из `primary_location.source`."""
    if not isinstance(location, dict):
        return ""
    source = location.get("source")
    return _text(source.get("display_name")) if isinstance(source, dict) else ""


def _pdf_url(work: dict[str, Any]) -> str:
    """Ссылка на открытый полный текст: сначала лучшая открытая копия, затем
    основное размещение. Обе ссылки в OpenAlex необязательные."""
    for key in ("best_oa_location", "primary_location"):
        location = work.get(key)
        if isinstance(location, dict) and (pdf_url := _text(location.get("pdf_url"))):
            return pdf_url
    return ""


def _authors(authorships: Any) -> list[str]:
    """Имена авторов в порядке выдачи источника."""
    if not isinstance(authorships, list):
        return []
    names: list[str] = []
    for authorship in authorships[:MAX_AUTHORS]:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        name = _text(author.get("display_name")) if isinstance(author, dict) else ""
        if name:
            names.append(name)
    return names


def _year(value: Any) -> int | None:
    """`publication_year` приходит целым, но у части записей его нет вовсе."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _abstract_from_inverted_index(value: Any) -> str:
    """Собрать аннотацию из инвертированного индекса OpenAlex.

    В ответе лежит {"слово": [позиции]} — прямого текста аннотации у источника
    нет вовсе, издатели запрещают его отдавать целиком. Одно слово может стоять
    в нескольких местах, поэтому пары «позиция → слово» раскладываются и
    сортируются по позиции. Дыры в нумерации (издатель вырезал часть текста)
    ничего не ломают: сортировка сохраняет порядок оставшихся слов.
    """
    if not isinstance(value, dict) or not value:
        return ""
    positions: list[tuple[int, str]] = []
    for word, places in value.items():
        if not isinstance(places, list):
            continue
        for place in places:
            if isinstance(place, int) and not isinstance(place, bool):
                positions.append((place, str(word)))
    if not positions:
        return ""
    positions.sort()
    return " ".join(word for _, word in positions).strip()[:MAX_SNIPPET]
