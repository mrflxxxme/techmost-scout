"""Яндекс: разбор выдачи и выбор контура — без сети и без состояния адаптера.

Вынесено из `search/yandex_search.py` (Д-5 волны Э2-З): адаптер занимается
асинхронным вызовом API и опросом операции, этот модуль — только выбором
контура/сужения формулировки и разбором уже полученного XML в кандидатов.
Единственное отступление от «без побочных эффектов» — `extract_root` поднимает
`SearchError`, когда сам Яндекс сообщает об отказе внутри XML (не кодом HTTP);
это разбор ответа, а не сетевой вызов, и место ему тут же, рядом с остальным
чтением дерева.
"""

from __future__ import annotations

import logging
from typing import Any

from defusedxml import ElementTree as ET  # noqa: N817 — ET общепринятое сокращение; defusedxml защищает от XML-атак
from pydantic import ValidationError

from app.schemas import Candidate, SearchQuery, SourceType
from app.search.base import SearchError, normalize_url

log = logging.getLogger(__name__)

MAX_SNIPPET = 700

#: Язык формулировки → контур выдачи
SEARCH_TYPE_BY_LANG = {"ru": "SEARCH_TYPE_RU", "en": "SEARCH_TYPE_COM"}

#: Контур выдачи → метка для счётчика кандидатов в оркестраторе (РЕШ-31).
#: Метка живёт рядом с правилом, которое выбирает сам контур, — так они не
#: могут разъехаться, а оркестратору не нужен частный случай под этот источник.
CONTOUR_LABEL_BY_SEARCH_TYPE = {"SEARCH_TYPE_RU": "yandex-ru", "SEARCH_TYPE_COM": "yandex-com"}

#: Где искать гранты: у реестров РНФ и Фонда содействия инновациям нет API, и
#: сузить выдачу до них — единственный способ вытащить поддержанные проекты.
#: Ограничение применяется ТОЛЬКО к российскому контуру и ТОЛЬКО к
#: web-формулировкам с грантовым намерением (маркеры — GRANT_INTENT_MARKERS
#: ниже): для остального `site:` отрезал бы всё живое, ради чего и нужен
#: открытый веб. Список держим здесь, а не в data/: две записи, и они следуют из
#: смысла источника, а не из подкрутки качества.
GRANT_SITES = ("rscf.ru", "fasie.ru")

#: Признак грантового намерения в русской web-формулировке (РЕШ-39, РЕШ-41).
#: РЕШ-173 вернула тип `grant` планировщику: основной путь сужения теперь —
#: `query.source_type is SourceType.GRANT` (см. `with_site_filter`), а этот
#: маркерный разбор остался только страховкой на случай, если модель вопреки
#: правилу 9 `planner.md` всё же разметит грантовую формулировку типом `web`
#: (РЕШ-109 запрещает считать послушание модели гарантией).
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
#: уходит БЕЗ сужения (см. with_site_filter, has_contest_outcome_marker).
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


def search_type_for(query: SearchQuery) -> str:
    """Контур выдачи по языку формулировки — единственное место правила."""
    return SEARCH_TYPE_BY_LANG.get(query.lang, "SEARCH_TYPE_RU")


def contour_label_for(search_type: str) -> str:
    """Метка контура для оркестратора: `yandex-ru` или `yandex-com`."""
    return CONTOUR_LABEL_BY_SEARCH_TYPE[search_type]


def has_contest_outcome_marker(text: str) -> bool:
    """Есть ли в тексте формулировки якорь итогов конкурса (РЕШ-117).

    Публичная функция, а не частность `with_site_filter`: тем же правилом
    гарантирует код `app.pipeline.stages.normalize_contest_queries` (РЕШ-121)
    - опознание конкурсной формулировки "площадки" против "итоги" обязано
    быть ровно одно на обе стороны, иначе они разошлись бы по смыслу.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in CONTEST_OUTCOME_MARKERS)


def with_site_filter(query: SearchQuery, search_type: str) -> str:
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
    * `grant` — по реестрам грантовых фондов (`GRANT_SITES`), и только в
      российском контуре (РЕШ-173): тип запроса уже несёт грантовое намерение,
      разбирать текст на маркеры не нужно — правило 9 `planner.md` требует
      `lang: "ru"` для этого типа, поэтому в мировом контуре он и не должен
      появляться, но проверка контура остаётся на случай ошибки модели.
    * `web` — тот же список сайтов, и только в российском контуре и только
      при грантовом намерении в тексте (`GRANT_INTENT_MARKERS`, РЕШ-41). Это
      страховка, а не основной путь: РЕШ-109 запрещает считать послушание
      модели гарантией, и если она вопреки правилу 9 разметит грантовую
      формулировку типом `web` вместо `grant`, сужение всё равно сработает.
      Конкурсные площадки сюда не попадают: слово «конкурс» есть и среди
      грантовых маркеров, и подмешивание площадок увело бы грантовый запрос
      из реестров РНФ и Фонда содействия.

    Для всего остального ограничение по сайтам убило бы смысл открытого веба.
    """
    if query.source_type is SourceType.CONTEST:
        if has_contest_outcome_marker(query.text):
            return query.text
        return _narrowed(query.text, CONTEST_SITES)
    if query.source_type is SourceType.GRANT:
        if search_type != "SEARCH_TYPE_RU":
            return query.text
        return _narrowed(query.text, GRANT_SITES)
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


def extract_root(raw: bytes) -> Any | None:
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


def to_candidates(root: Any, *, limit: int, source_type: SourceType, provider: str) -> list[Candidate]:
    """Все документы выдачи → кандидаты, без повторов по нормализованной ссылке."""
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for doc in root.findall(".//doc"):
        candidate = _to_candidate(doc, source_type, provider)
        if candidate is None or candidate.raw_id in seen:
            continue
        seen.add(candidate.raw_id or "")
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def _to_candidate(doc: Any, source_type: SourceType, provider: str) -> Candidate | None:
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
            # следует, обязана стадия `extractor` (см. `raw_records` ниже);
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
            provider=provider,
        )
    except ValidationError as exc:
        log.debug("yandex_search: запись отброшена (%s): %s", url, exc)
        return None


def raw_records(root: Any, *, limit: int) -> list[dict]:
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
