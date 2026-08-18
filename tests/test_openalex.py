"""OpenAlex: русскоязычный поиск (Т-15), обогащение по DOI и таблица доктора (Т-3).

Сети здесь нет: ответы OpenAlex подменяются через respx (инвариант №7).
Проверяется ровно то, что может сломаться молча: ритм обхода, отказ, который
обязан быть виден, правило «непустое поле кандидата не перезаписывается», а с
Т-15 — ещё и фильтр языка, вежливый режим и разворот реферата из
инвертированного индекса.

У адаптера две роли на одном файле, поэтому тесты обогащения по DOI при
подключении поиска не переписывались: их зелёный цвет — доказательство, что
роль Т-3 не задета.
"""

from __future__ import annotations

import io
import logging
import time
from datetime import UTC, date, datetime
from typing import ClassVar

import httpx
import pytest
import respx
from rich.console import Console

import app.pipeline as pipeline_pkg
from app import cli
from app.cli import SECRET_CHECKS
from app.llm import LLMRouter
from app.pipeline import orchestrator, stages
from app.pipeline.orchestrator import ScoutPipeline
from app.pipeline.persistence import StageRecord
from app.schemas import Brief, Candidate, IntakeResult, Report, ReportSummary, SearchPlan, SearchQuery, SourceType
from app.search import ADAPTERS, REQUIRED_SETTINGS, STATUS_NO_KEY, STATUS_STUB, adapter_status, build_adapters
from app.search.base import SearchAdapter, SearchError
from app.search.crossref import CONTACT_EMAIL
from app.search.openalex import (
    API_URL,
    BATCH_SIZE,
    FREE_DAILY_USD,
    FRESHNESS_YEARS,
    MAX_SNIPPET,
    USD_PER_SEARCH,
    OpenAlexAdapter,
    _abstract_from_inverted_index,
    normalize_doi,
)

DOI = "10.1234/example-a"
LOOKUP = f"{API_URL}/doi:"


def _work(
    *,
    org: str | None = "Институт катализа СО РАН",
    pdf_url: str | None = "https://example.org/full-text.pdf",
    cited_by_count: int | None = 42,
    abstract: bool = True,
) -> dict:
    """Ответ OpenAlex на lookup по DOI — в том виде, в каком его отдаёт API."""
    payload: dict = {"id": "https://openalex.org/W1", "doi": f"https://doi.org/{DOI}"}
    if org:
        payload["authorships"] = [
            {"author": {"display_name": "Иванов И."}, "institutions": []},
            {
                "author": {"display_name": "Петров П."},
                "institutions": [{"display_name": org, "ror": "https://ror.org/1"}],
            },
        ]
    if abstract:
        # Прямого текста аннотации у OpenAlex нет — только инвертированный индекс
        payload["abstract_inverted_index"] = {"Катализатор": [0], "сохраняет": [1], "активность": [2]}
    if pdf_url:
        payload["best_oa_location"] = {"is_oa": True, "pdf_url": pdf_url}
    if cited_by_count is not None:
        payload["cited_by_count"] = cited_by_count
    return payload


def _adapter(client: httpx.AsyncClient, *, api_key: str | None = "test-key") -> OpenAlexAdapter:
    return OpenAlexAdapter(client, api_key=api_key)


def _query(*, text: str = "гидроочистка дизельного топлива", lang: str = "ru") -> SearchQuery:
    return SearchQuery(text=text, source_type=SourceType.PUBLICATION, lang=lang, rationale="тест")


def _work_hit(
    *,
    display_name: str | None = "ОПТИМИЗАЦИИ УСТАНОВКИ ГИДРООЧИСТКИ ДИЗЕЛЬНОГО ТОПЛИВА ТОО «ПНХЗ»",
    doi: str | None = "https://doi.org/10.48081/vgcj1519",
    landing_page_url: str | None = "https://vestnik.example.kz/vgcj1519",
    pdf_url: str | None = "https://example.org/vgcj1519.pdf",
    work_id: str | None = "https://openalex.org/W4383",
    abstract: bool = True,
) -> dict:
    """Одна работа поисковой выдачи в том виде, в каком её отдаёт `GET /works`.

    Слепок сделан с живого ответа разведки (`.planning/evidence/E2A-ru-science.md`,
    §2.1): та самая статья 2023 года, найденная на запросе, где КиберЛенинка
    дала ноль.
    """
    work: dict = {
        "display_name": display_name,
        "publication_year": 2023,
        "type": "article",
        "authorships": [
            {
                "author": {"display_name": "Сериков Т. П."},
                "institutions": [{"display_name": "Торайгыров университет", "ror": "https://ror.org/1"}],
            }
        ],
    }
    if work_id:
        work["id"] = work_id
    if doi:
        work["doi"] = doi
    location: dict = {"source": {"display_name": "Bulletin of Toraighyrov University Chemistry & Biology series"}}
    if landing_page_url:
        location["landing_page_url"] = landing_page_url
    work["primary_location"] = location
    if pdf_url:
        work["best_oa_location"] = {"is_oa": True, "pdf_url": pdf_url}
    if abstract:
        # Прямого текста реферата у OpenAlex нет — только инвертированный индекс
        work["abstract_inverted_index"] = {
            "Рассмотрена": [0],
            "оптимизация": [1],
            "установки": [2],
            "гидроочистки": [3],
            "дизельного": [4],
            "топлива": [5],
        }
    return work


def _search_payload(*works: dict) -> dict:
    hits = list(works) or [_work_hit()]
    return {"meta": {"count": len(hits), "per_page": len(hits)}, "results": hits}


def _candidate(*, raw_id: str | None = DOI, **fields) -> Candidate:
    base = {
        "source_type": SourceType.PUBLICATION,
        "title": "Стабилизация катализаторов гидроочистки",
        "url": "https://doi.org/10.1234/example-a",
        "provider": "crossref",
        "raw_id": raw_id,
    }
    base.update(fields)
    return Candidate(**base)


# ── Патч ────────────────────────────────────────────────────────────────────
@respx.mock
async def test_патч_несёт_организацию_аннотацию_pdf_и_цитируемость():
    """РЕШ-50: тот же бесплатный lookup отдаёт и PDF, и цитируемость — оба поля
    обязаны доехать до патча, иначе расширение контракта существует только на
    бумаге."""
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))

    async with httpx.AsyncClient() as client:
        patches = await _adapter(client).enrich_by_doi([DOI])

    patch = patches[DOI]
    assert patch["org"] == "Институт катализа СО РАН"
    assert patch["snippet"] == "Катализатор сохраняет активность"
    assert patch["pdf_url"] == "https://example.org/full-text.pdf"
    assert patch["cited_by_count"] == 42


@respx.mock
async def test_пустых_значений_в_патче_не_бывает():
    """Поля, которых источник не дал, в патч не попадают вовсе: пустая строка
    затёрла бы честное «неизвестно» кандидата (инвариант №1)."""
    respx.get(url__startswith=LOOKUP).mock(
        return_value=httpx.Response(200, json=_work(org=None, pdf_url=None, cited_by_count=None, abstract=False))
    )

    async with httpx.AsyncClient() as client:
        patches = await _adapter(client).enrich_by_doi([DOI])

    assert patches == {}, f"патч из пустых полей: {patches}"


@respx.mock
async def test_ключ_ответа_нормализован_независимо_от_написания_doi():
    """DOI приходят из разных источников в разном написании — в ответе они
    обязаны быть одинаковыми, иначе патч не найдёт своего кандидата."""
    route = respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))

    async with httpx.AsyncClient() as client:
        patches = await _adapter(client).enrich_by_doi([f"https://doi.org/{DOI.upper()}", f"doi:{DOI}", DOI])

    assert list(patches) == [DOI]
    assert route.call_count == 1, "один и тот же DOI запрошен несколько раз"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1234/Example-A", "10.1234/example-a"),
        ("https://doi.org/10.1234/example-a", "10.1234/example-a"),
        ("  doi:10.1234/example-a ", "10.1234/example-a"),
        ("RU2799999", ""),
        (None, ""),
    ],
)
def test_нормализация_doi(raw, expected):
    assert normalize_doi(raw) == expected


# ── Ритм обхода ─────────────────────────────────────────────────────────────
@respx.mock
async def test_обход_идёт_батчами_по_50_с_паузой_между_ними(monkeypatch):
    """Батч 50 и пауза между батчами — контракт SPEC §4 Э2-1. 120 DOI — это три
    батча и ровно две паузы; сами lookup идут по одному, они бесплатны."""
    route = respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))
    dois = [f"10.1234/work-{number}" for number in range(120)]
    pauses = 0

    async def count_pause() -> None:
        nonlocal pauses
        pauses += 1

    async def no_throttle() -> None:
        """Троттлинг внутри батча проверяется отдельным тестом; здесь он выключен,
        иначе 120 lookup растянулись бы на шесть секунд набора."""

    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        monkeypatch.setattr(adapter, "_pause_between_batches", count_pause)
        monkeypatch.setattr(adapter, "_throttle", no_throttle)
        patches = await adapter.enrich_by_doi(dois)

    assert BATCH_SIZE == 50
    assert route.call_count == 120
    assert pauses == 2, "пауза обязана быть между батчами, а не после каждого запроса"
    assert len(patches) == 120


@respx.mock
async def test_внутри_батча_запросы_идут_не_подряд():
    """Пауза между батчами не защищает от 429 внутри батча.

    До правки `min_interval_seconds` у адаптера не был задан, а базовый
    `_throttle` при нуле возвращается сразу — пятьдесят запросов уходили за
    доли секунды, и «вежливость» из комментария существовала только на бумаге
    (П-4 аудита). Проверяем фактом: три lookup подряд обязаны занять не меньше
    двух интервалов.
    """
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))

    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        assert adapter.min_interval_seconds > 0, "троттлинг адаптера — пустая операция"
        started = time.perf_counter()
        await adapter.enrich_by_doi(["10.1234/work-a", "10.1234/work-b", "10.1234/work-c"])
        elapsed = time.perf_counter() - started

    # Первый запрос не ждёт (ждать не от чего), поэтому интервалов ровно два.
    # Допуск на разрешение системного таймера — отсюда 0,9.
    assert elapsed >= 2 * adapter.min_interval_seconds * 0.9, f"запросы ушли подряд, за {elapsed:.3f} с"


# ── Отказы ──────────────────────────────────────────────────────────────────
@respx.mock
async def test_без_ключа_пустой_словарь_и_ни_одного_обращения_в_сеть(caplog):
    """Ключа нет — обогащение пропускается с записью в лог и без падения."""
    route = respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))

    async with httpx.AsyncClient() as client:
        patches = await _adapter(client, api_key=None).enrich_by_doi([DOI])

    assert patches == {}
    assert route.call_count == 0, "без ключа адаптер обязан молчать, а не ходить в сеть"
    assert any("нет OPENALEX_API_KEY" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("status", [429, 500])
@respx.mock
async def test_транспортный_отказ_поднимается_как_search_error(status):
    """Инвариант №5 в редакции РЕШ-43: адаптер честно говорит «не смог», а не
    отдаёт пустой успех. Гашение отказа здесь скрыло бы его от отчёта."""
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(status, json={"error": "нет"}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await _adapter(client).enrich_by_doi([DOI])


@respx.mock
async def test_нечитаемое_тело_ответа_тоже_отказ():
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, text="не json"))

    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await _adapter(client).enrich_by_doi([DOI])


@respx.mock
async def test_отказ_в_середине_обхода_не_стирает_собранное(caplog):
    """Один 429 на середине не отменяет уже собранное обогащение.

    При 50–100 последовательных lookup один отказ — не гипотеза: именно 429 на
    каждом запросе трёх прогонов и был исходным уроком инварианта №5. Раньше
    `SearchError` уходил наружу из середины цикла и уносил все накопленные
    патчи (П-4 аудита). Теперь обход прекращается, частичный результат
    остаётся, а причина видна и в логе, и в `last_error`.
    """
    respx.get(url__startswith=LOOKUP).mock(
        side_effect=[httpx.Response(200, json=_work()), httpx.Response(429, json={"error": "слишком часто"})]
    )

    with caplog.at_level(logging.WARNING):
        async with httpx.AsyncClient() as client:
            adapter = _adapter(client)
            patches = await adapter.enrich_by_doi([DOI, "10.1234/example-b"])

    assert list(patches) == [DOI], "собранное до отказа обогащение потеряно"
    assert adapter.last_error and "10.1234/example-b" in adapter.last_error
    assert any("обход прерван" in record.getMessage() for record in caplog.records), "обрыв обхода не виден в прогоне"


@respx.mock
async def test_отказ_на_первом_же_doi_остаётся_отказом():
    """Обратная сторона частичного результата: собрать ничего не успели —
    значит это отказ источника целиком, и он обязан дойти до оркестратора, а не
    превратиться в пустой успех (инвариант №5)."""
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(429, json={"error": "слишком часто"}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await _adapter(client).enrich_by_doi([DOI, "10.1234/example-b"])


@respx.mock
async def test_работы_нет_в_каталоге_это_не_отказ():
    """404 на конкретный DOI — успешный ответ источника о том, что работы у него
    нет. Отказом это не считается (инвариант №5), патча просто не будет."""
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(404, json={"error": "Not found"}))

    async with httpx.AsyncClient() as client:
        assert await _adapter(client).enrich_by_doi([DOI]) == {}


# ── Поиск (Т-15, РЕШ-71) ────────────────────────────────────────────────────
@respx.mock
async def test_русская_выдача_разбирается_в_кандидатов():
    """Т-15 целиком на одной записи: русскоязычная работа обязана доехать до
    кандидата со всем, что источник о ней сказал.

    Это ровно тот материал, которого не хватило прогону 16 августа: там
    единственный русскоязычный источник дал ноль (`cyberleninka: 0`), а на том
    же запросе OpenAlex находит статьи по теме.
    """
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, json=_search_payload()))

    async with httpx.AsyncClient() as client:
        found = await _adapter(client).search(_query(), limit=8)

    assert len(found) == 1
    candidate = found[0]
    assert candidate.title == "ОПТИМИЗАЦИИ УСТАНОВКИ ГИДРООЧИСТКИ ДИЗЕЛЬНОГО ТОПЛИВА ТОО «ПНХЗ»"
    assert candidate.year == 2023
    assert candidate.raw_id == "10.48081/vgcj1519"
    assert str(candidate.url) == "https://doi.org/10.48081/vgcj1519"
    assert str(candidate.pdf_url) == "https://example.org/vgcj1519.pdf"
    assert candidate.org == "Торайгыров университет"
    assert candidate.authors == ["Сериков Т. П."]
    assert candidate.provider == "openalex"
    assert candidate.source_type is SourceType.PUBLICATION
    # Реферат — вход скоринга: без него кандидат нечем оценивать
    assert "Рассмотрена оптимизация установки гидроочистки" in candidate.snippet
    assert "Bulletin of Toraighyrov University" in candidate.snippet


@respx.mock
async def test_фильтр_языка_и_свежести_уходит_в_запрос():
    """Существо задачи: без `language:ru` взвешенный поиск отдаёт на русский
    запрос преимущественно англоязычные работы, и русской науки в отчёте снова
    нет. Язык берётся из формулировки плана, а не прибит к «ru»."""
    route = respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, json=_search_payload()))

    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.search(_query(), limit=8)
        await adapter.search(_query(lang="en"), limit=8)

    russian = dict(route.calls[0].request.url.params)
    assert russian["search"] == "гидроочистка дизельного топлива"
    assert russian["filter"] == f"language:ru,from_publication_date:{date.today().year - FRESHNESS_YEARS}-01-01"
    assert russian["per-page"] == "8"
    assert "abstract_inverted_index" in russian["select"], "без реферата нечего показывать скорингу"

    english = dict(route.calls[1].request.url.params)
    assert english["filter"].startswith("language:en,"), "язык формулировки подменён на русский"


@respx.mock
async def test_вежливый_режим_почта_владельца_в_параметре_и_в_заголовке():
    """Вежливый режим OpenAlex — `mailto`. Дублируется в `User-Agent` по той же
    причине, что и у Crossref: часть прокси срезает заголовок."""
    route = respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, json=_search_payload()))

    async with httpx.AsyncClient() as client:
        await _adapter(client).search(_query(), limit=5)

    request = route.calls[0].request
    assert dict(request.url.params)["mailto"] == CONTACT_EMAIL
    assert CONTACT_EMAIL in request.headers["User-Agent"]
    assert "Authorization" not in request.headers, "поиск бесплатен, ключ ему не нужен"


@respx.mock
async def test_поиск_работает_и_без_ключа():
    """РЕШ-71: ключа поиск не требует. Пока источник был `requires_key = True`,
    он не попадал в сборку вовсе — русской выдачи не появилось бы даже с
    реализованным `search`."""
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, json=_search_payload()))

    async with httpx.AsyncClient() as client:
        found = await _adapter(client, api_key=None).search(_query(), limit=5)

    assert len(found) == 1


@respx.mock
async def test_работа_без_doi_держится_на_посадочной_странице():
    """Часть русскоязычного корпуса приходит из репозиториев без DOI (у разведки
    это записи DOAJ и вузовских архивов). Ссылка обязана найтись, иначе годная
    работа выбрасывается."""
    work = _work_hit(doi=None, landing_page_url="https://elib.example.ru/item/158931")
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, json={"results": [work]}))

    async with httpx.AsyncClient() as client:
        found = await _adapter(client).search(_query(), limit=5)

    assert str(found[0].url) == "https://elib.example.ru/item/158931"
    assert found[0].raw_id is None


@respx.mock
async def test_запись_без_названия_и_без_ссылки_отбрасывается():
    """Кандидат без названия или без ссылки бесполезен: карточку нечем
    подтвердить. Отбрасывается запись, а не вся выдача."""
    respx.get(url__startswith=API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _work_hit(display_name=""),
                    _work_hit(doi=None, landing_page_url=None, work_id=None),
                    _work_hit(),
                ]
            },
        )
    )

    async with httpx.AsyncClient() as client:
        found = await _adapter(client).search(_query(), limit=5)

    assert len(found) == 1, "негодная запись унесла с собой годную"


@respx.mock
async def test_негодная_ссылка_на_pdf_не_уносит_кандидата(caplog):
    """Тот же принцип, что П-2 аудита завела для обогащения: отбрасывается
    ПОЛЕ, а не вся находка."""
    respx.get(url__startswith=API_URL).mock(
        return_value=httpx.Response(200, json={"results": [_work_hit(pdf_url="не ссылка")]})
    )

    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient() as client:
            found = await _adapter(client).search(_query(), limit=5)

    assert found[0].pdf_url is None
    assert found[0].title, "кандидат выброшен вместе с кривой ссылкой"
    assert any("поле pdf_url отброшено" in record.getMessage() for record in caplog.records)


@respx.mock
async def test_пустая_выдача_это_не_отказ():
    """Инвариант №5: источник ответил и сказал «ничего нет» — обычный пустой
    список. Превращать это в отказ значит врать в `sources_unavailable`."""
    respx.get(url__startswith=API_URL).mock(
        return_value=httpx.Response(200, json={"meta": {"count": 0}, "results": []})
    )

    async with httpx.AsyncClient() as client:
        assert await _adapter(client).search(_query(), limit=5) == []


@pytest.mark.parametrize("status", [429, 500])
@respx.mock
async def test_отказ_поиска_поднимается_как_search_error(status):
    """Инвариант №5 в редакции РЕШ-43: транспортный отказ обязан дойти до
    оркестратора отказом, а не пустым успехом."""
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(status, json={"error": "нет"}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await _adapter(client).search(_query(), limit=5)


@respx.mock
async def test_нечитаемый_ответ_поиска_тоже_отказ():
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, text="не json"))

    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await _adapter(client).search(_query(), limit=5)


# ── Инвертированный индекс реферата ─────────────────────────────────────────
@pytest.mark.parametrize(
    ("index", "expected"),
    [
        ({"Катализатор": [0], "сохраняет": [1], "активность": [2]}, "Катализатор сохраняет активность"),
        # Пустой индекс и его отсутствие — не ошибка: реферата просто нет
        ({}, ""),
        (None, ""),
        ("текст реферата", ""),
        # Слово встречается дважды — обе позиции обязаны встать на свои места
        ({"и": [1, 3], "кокс": [0], "сера": [2], "азот": [4]}, "кокс и сера и азот"),
        # Дыры в нумерации: издатель вырезал часть текста, порядок оставшегося
        # обязан сохраниться
        ({"начало": [0], "конец": [17]}, "начало конец"),
        # Позиции не числом — запись мусорная, слово в текст не идёт
        ({"хорошо": [0], "плохо": "нет", "тоже": [None, 1]}, "хорошо тоже"),
        # Ни одной годной позиции — реферата нет
        ({"слово": []}, ""),
    ],
)
def test_разворот_инвертированного_индекса(index, expected):
    """Прямого текста реферата OpenAlex не отдаёт вовсе — только индекс
    «слово → позиции». Разворот тихо не ломается: любая порча даёт пустой
    реферат, а не мусор в карточке клиента."""
    assert _abstract_from_inverted_index(index) == expected


def test_реферат_режется_по_той_же_границе_что_у_crossref():
    """Вход скоринга не резиновый: длинный реферат обязан быть обрезан."""
    index = {f"слово{number}": [number] for number in range(500)}

    assert len(_abstract_from_inverted_index(index)) == MAX_SNIPPET


# ── Поиск в прогоне ─────────────────────────────────────────────────────────
@respx.mock
async def test_оркестратор_спрашивает_openalex_поиском(settings):
    """Обратная сторона снятой пометки «только обогащение»: источник ОБЯЗАН быть
    опрошен поиском. Адаптер, который ищет, но которого никто не зовёт, ничем
    не отличается от отсутствующего."""
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, json=_search_payload()))
    plan = SearchPlan(queries=[_query()])

    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=LLMRouter(settings), adapters=[_adapter(client)])
        outcome = await pipeline.gather_candidates(plan)

    assert [candidate.provider for candidate in outcome.candidates] == ["openalex"]
    assert outcome.unavailable == []
    assert outcome.hits_by_provider == {"openalex": 1}


@respx.mock
async def test_отказ_поиска_доезжает_до_недоступных_источников(settings):
    """Отказ поиска обязан быть виден клиенту в `sources_unavailable`, а не
    выглядеть как «источник ничего не нашёл»."""
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(500, json={"error": "нет"}))
    plan = SearchPlan(queries=[_query()])

    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=LLMRouter(settings), adapters=[_adapter(client)])
        outcome = await pipeline.gather_candidates(plan)

    assert outcome.candidates == []
    assert outcome.unavailable == ["openalex"]


# ── Склейка патча с кандидатом (оркестратор) ────────────────────────────────
async def _enrich(settings, adapter, candidates: list[Candidate]):
    pipeline = ScoutPipeline(settings=settings, router=LLMRouter(settings), adapters=[adapter])
    return await pipeline.enrich_candidates(candidates)


@respx.mock
async def test_патч_заполняет_пустые_поля_и_отдаёт_цитируемость(settings):
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))
    candidate = _candidate(org=None, snippet="", pdf_url=None)

    async with httpx.AsyncClient() as client:
        enriched, citations = await _enrich(settings, _adapter(client), [candidate])

    assert enriched[0].org == "Институт катализа СО РАН"
    assert enriched[0].snippet == "Катализатор сохраняет активность"
    assert str(enriched[0].pdf_url) == "https://example.org/full-text.pdf"
    assert citations == {candidate.dedup_key(): 42}


@respx.mock
async def test_непустые_поля_кандидата_не_перезаписываются(settings):
    """Существо правки: источник, нашедший кандидата, видел его в контексте
    своей выдачи, а обогащение работает вслепую по DOI."""
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))
    candidate = _candidate(
        org="НИИ нефтехимии",
        snippet="[проверенный партнёр] Пилот на двух установках.",
        pdf_url="https://example.org/own.pdf",
    )

    async with httpx.AsyncClient() as client:
        enriched, citations = await _enrich(settings, _adapter(client), [candidate])

    assert enriched[0].org == "НИИ нефтехимии"
    assert enriched[0].snippet == "[проверенный партнёр] Пилот на двух установках."
    assert str(enriched[0].pdf_url) == "https://example.org/own.pdf"
    # Цитируемость полем кандидата не является и потому доезжает всегда
    assert citations == {candidate.dedup_key(): 42}


@respx.mock
async def test_прогон_считает_скольким_кандидатам_заполнен_org(settings, caplog):
    """П-1 аудита: эффект обогащения на состав карточек обязан быть измерим.

    Карточки группируются по ТОЧНОЙ строке `org`, а OpenAlex часто отдаёт
    зонтичную организацию вместо лаборатории: заполнение пустого `org` способно
    схлопнуть две несвязанные работы в одну карточку. Считается именно
    заполнение пустого поля — у кандидата с уже известной организацией ничего
    не меняется.
    """
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))
    empty = _candidate(org=None)
    known = _candidate(raw_id="10.1234/example-b", url="https://doi.org/10.1234/example-b", org="НИИ нефтехимии")

    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient() as client:
            enriched, _ = await _enrich(settings, _adapter(client), [empty, known])

    assert enriched[0].org == "Институт катализа СО РАН" and enriched[1].org == "НИИ нефтехимии"
    messages = [record.getMessage() for record in caplog.records]
    assert any("заполнен пустой org у 1 кандидатов из 2" in message for message in messages), messages


@respx.mock
async def test_ноль_цитирований_закреплён_у_производителя_патча(settings):
    """Ноль — известное значение, а не «источник промолчал».

    У потребителя (`candidates_listing`) это закреплено тестом, у производителя
    не было закреплено ничем: `cited >= 0` тихо превращается в `cited > 0`, и
    работа с нулём цитирований становится неотличима от работы, о цитируемости
    которой ничего не известно (П-3 аудита). Проверяются оба конца: ключ в
    патче и ноль в словаре цитируемости, который уезжает в скоринг.
    """
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work(cited_by_count=0)))
    candidate = _candidate(org=None)

    async with httpx.AsyncClient() as client:
        patches = await _adapter(client).enrich_by_doi([DOI])
        enriched, citations = await _enrich(settings, _adapter(client), [candidate])

    assert "cited_by_count" in patches[DOI], "ноль потерян уже в патче адаптера"
    assert patches[DOI]["cited_by_count"] == 0
    assert citations == {candidate.dedup_key(): 0}, "ноль не доехал до скоринга"
    assert enriched[0].org == "Институт катализа СО РАН"


@respx.mock
async def test_негодное_поле_патча_не_уносит_годные(settings, caplog):
    """П-2 аудита: отбрасывается ПОЛЕ, а не патч целиком.

    Мусорная ссылка обязана отвалиться в одиночку: организация и аннотация из
    того же ответа — годные данные, и терять их вместе со ссылкой значит
    сходить за обогащением и выбросить его результат. Сообщение об
    отбрасывании обязано быть видно в обычном прогоне: на `DEBUG` его не видел
    никто.
    """
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work(pdf_url="не ссылка")))
    candidate = _candidate(org=None, snippet="", pdf_url=None)

    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient() as client:
            enriched, citations = await _enrich(settings, _adapter(client), [candidate])

    assert enriched[0].org == "Институт катализа СО РАН", "годная организация выброшена вместе с кривой ссылкой"
    assert enriched[0].snippet == "Катализатор сохраняет активность"
    assert enriched[0].pdf_url is None, "негодная ссылка всё же доехала до кандидата"
    assert citations == {candidate.dedup_key(): 42}
    messages = [record.getMessage() for record in caplog.records]
    assert any("поле pdf_url отброшено" in message for message in messages), "отбрасывание поля не видно в прогоне"
    assert any("отброшено негодных полей патча — 1" in message for message in messages)


@respx.mock
async def test_кандидат_без_doi_обогащение_не_запрашивает(settings):
    route = respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))
    patent = _candidate(raw_id="RU2799999", url="https://patents.example/RU2799999", source_type=SourceType.PATENT)

    async with httpx.AsyncClient() as client:
        enriched, citations = await _enrich(settings, _adapter(client), [patent])

    assert route.call_count == 0
    assert enriched == [patent] and citations == {}


class _FailingEnricher:
    """Источник обогащения, который отказывает целиком."""

    name = "openalex"

    async def enrich_by_doi(self, dois: list[str]) -> dict:
        raise SearchError("openalex не ответил на lookup DOI")


async def test_отказ_обогащения_не_роняет_прогон(settings, caplog):
    """Адаптер поднимает `SearchError`, оркестратор решает продолжать: отказ
    виден в логе, кандидаты остаются прежними (РЕШ-52.3)."""
    candidate = _candidate(org=None)

    enriched, citations = await _enrich(settings, _FailingEnricher(), [candidate])

    assert enriched == [candidate] and citations == {}
    assert any("обогащение по DOI не удалось" in record.getMessage() for record in caplog.records)


async def test_отказ_обогащения_оставляет_строку_в_журнале_стадий(settings):
    """П-5 аудита: отказ обязан быть виден не только в логе прогона.

    Лог живёт до конца процесса, а `pipeline_logs` — след, по которому видно,
    что обогащение отказывает в каждом прогоне. Без строки с `error`
    отказывающее месяц подряд обогащение снаружи неотличимо от работающего.
    Тот же принцип Т-2 завела для интейка.
    """
    stage_log: list[StageRecord] = []
    pipeline = ScoutPipeline(settings=settings, router=LLMRouter(settings), adapters=[_FailingEnricher()])

    await pipeline.enrich_candidates([_candidate(org=None)], stage_log=stage_log)

    record = next(item for item in stage_log if item.stage == "enrich")
    assert record.error and "обогащение по DOI не удалось" in record.error


@respx.mock
async def test_прерванный_обход_и_отброшенные_поля_тоже_попадают_в_журнал(settings):
    """Три пути тихой деградации — отказ целиком, прерванный обход и
    отброшенное поле патча — обязаны различаться в журнале, а не сливаться в
    молчание. Здесь проверяются два оставшихся."""
    respx.get(url__startswith=LOOKUP).mock(
        side_effect=[
            httpx.Response(200, json=_work(pdf_url="не ссылка")),
            httpx.Response(429, json={"error": "слишком часто"}),
        ]
    )
    first = _candidate(org=None, snippet="", pdf_url=None)
    second = _candidate(raw_id="10.1234/example-b", url="https://doi.org/10.1234/example-b", org=None)
    stage_log: list[StageRecord] = []

    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=LLMRouter(settings), adapters=[_adapter(client)])
        enriched, _ = await pipeline.enrich_candidates([first, second], stage_log=stage_log)

    record = next(item for item in stage_log if item.stage == "enrich")
    assert record.error and "обход прерван" in record.error, "обрыв обхода не доехал до журнала"
    assert "отброшено негодных полей патча" in record.error
    assert enriched[0].org == "Институт катализа СО РАН", "частичный результат потерян"


async def test_успешное_обогащение_пишет_стадию_без_ошибки(settings):
    """Строка стадии есть всегда — иначе «обогащения не было» и «обогащение
    прошло молча» в журнале выглядят одинаково."""
    stage_log: list[StageRecord] = []

    class _EmptyEnricher:
        name = "openalex"

        async def enrich_by_doi(self, dois: list[str]) -> dict:
            return {}

    pipeline = ScoutPipeline(settings=settings, router=LLMRouter(settings), adapters=[_EmptyEnricher()])
    await pipeline.enrich_candidates([_candidate(org=None)], stage_log=stage_log)

    record = next(item for item in stage_log if item.stage == "enrich")
    assert record.error is None


class _OneHitAdapter(SearchAdapter):
    """Источник, отдающий одного кандидата с DOI — на нём проверяется место
    обогащения в прогоне."""

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        return [_candidate(org=None, snippet="")]


@respx.mock
async def test_обогащение_встроено_между_дедупликацией_и_скорингом(settings, brief, monkeypatch):
    """РЕШ-52.3 целиком: кандидат доезжает до скоринга уже обогащённым, а
    цитируемость — вместе с ним.

    Проверка сквозная по прогону, а не по одному вызову: обогащение, которое
    работает, но не подключено к стадии-потребителю, ничем не отличается от
    отсутствующего. Тем же прогоном проверяется, что журнал стадий доезжает до
    записи: строка `enrich` в `pipeline_logs` — единственный след обогащения,
    остающийся после прогона (П-5 аудита).
    """
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))
    # Поиск у того же источника теперь тоже спрашивается (Т-15): маршрут lookup
    # зарегистрирован первым и потому забирает свои адреса себе, поиску
    # достаётся всё остальное. Выдача пустая намеренно — здесь проверяется место
    # обогащения в прогоне, и вторая находка только зашумила бы проверку.
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, json={"meta": {}, "results": []}))
    captured: dict = {}

    async def spy_persist(session_factory, **kwargs) -> None:
        captured["stages"] = kwargs["stages"]

    monkeypatch.setattr(orchestrator, "persist_run", spy_persist)

    async def fake_plan(router, brief_):
        return SearchPlan(
            queries=[SearchQuery(text="катализатор", source_type=SourceType.PUBLICATION, lang="ru", rationale="тест")]
        )

    async def spy_score(router, brief_, candidates, *, batch_size=10, citations=None):
        captured["candidates"] = candidates
        captured["citations"] = citations
        return []

    async def fake_summarize(router, brief_, cards):
        return ReportSummary(comparison_rows=[], recommendation="нечего рекомендовать", caveats=[])

    monkeypatch.setattr(stages, "plan_search", fake_plan)
    monkeypatch.setattr(stages, "score_candidates", spy_score)
    monkeypatch.setattr(stages, "summarize", fake_summarize)

    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(
            settings=settings,
            router=LLMRouter(settings),
            adapters=[_OneHitAdapter(client), _adapter(client)],
        )
        report = await pipeline.run(brief)

    scored_candidate = captured["candidates"][0]
    assert scored_candidate.org == "Институт катализа СО РАН", "скоринг получил необогащённого кандидата"
    assert captured["citations"] == {scored_candidate.dedup_key(): 42}, "цитируемость до скоринга не доехала"
    assert report.sources_unavailable == []
    assert "enrich" in [record.stage for record in captured["stages"]], "стадия обогащения не попала в журнал прогона"


# ── Доктор ──────────────────────────────────────────────────────────────────
async def test_доктор_видит_все_источники_реестра():
    """РЕШ-52.1: `include_stubs=True` показывает все источники — заготовки как
    «не реализовано», источники без ключей как «нет ключа»."""
    async with httpx.AsyncClient() as client:
        adapters = build_adapters(client, None, include_stubs=True)

    assert [a.name for a in adapters] == list(ADAPTERS), "таблица доктора обязана показывать весь реестр"
    statuses = {a.name: adapter_status(a) for a in adapters}
    assert statuses["crossref"] is None, "бесплатный рабочий источник статуса не имеет"
    # Роспатент с Т-14 — рабочий адаптер поисковой платформы, и без ключа он
    # выключается по той же причине, что openalex, а не как заготовка
    assert statuses["rospatent"][0] == STATUS_NO_KEY
    assert "ROSPATENT_API_KEY" in statuses["rospatent"][1]
    assert statuses["patents_view"][0] == STATUS_STUB
    assert statuses["yandex_search"][0] == STATUS_NO_KEY
    assert "YANDEX_SEARCH_API_KEY" in statuses["yandex_search"][1]
    assert "test" not in statuses["yandex_search"][1], "в пояснении не может быть значений ключей (инвариант №3)"


async def test_openalex_включён_и_без_ключа_а_ключ_доезжает_до_обогащения():
    """Т-15, РЕШ-71: поиск бесплатен, поэтому источник обязан быть в рабочем
    наборе без всяких настроек. Прежняя редакция (`requires_key = True`) делала
    поиск недостижимым: адаптер не попадал в сборку вовсе.

    Вторая половина — та, которую легко потерять: ключ, если он задан, обязан
    по-прежнему доехать до конструктора, иначе тихо отвалится обогащение по DOI.
    """
    async with httpx.AsyncClient() as client:
        without_settings = build_adapters(client, None)
        with_key = build_adapters(client, {"openalex_api_key": "k"})

    assert "openalex" in {a.name for a in without_settings}, "без ключа источник обязан искать"
    assert adapter_status(next(a for a in without_settings if a.name == "openalex")) is None
    assert next(a for a in with_key if a.name == "openalex")._api_key == "k", "ключ не доехал до обогащения"


async def test_рабочий_набор_прежний_заготовок_в_нём_нет():
    """Пайплайн зовёт `build_adapters` без `include_stubs` — там по-прежнему
    только то, что действительно работает."""
    async with httpx.AsyncClient() as client:
        names = {a.name for a in build_adapters(client, None)}

    assert "rospatent" not in names and "patents_view" not in names
    assert "yandex_search" not in names, "без ключа платный источник не включается"
    assert "crossref" in names


class _DoctorAdapter:
    """Источник для проверок доктора: помнит, спрашивали ли у него здоровье."""

    def __init__(self, name: str, status: tuple[str, str] | None = None) -> None:
        self.name = name
        #: Тот же атрибут, который ставит `build_adapters` и читает `adapter_status`
        self.disabled_status = status
        self.health_calls = 0

    async def health(self) -> tuple[bool, str]:
        self.health_calls += 1
        return True, "ок, проба выполнена"


async def _run_doctor(monkeypatch, adapters: list[_DoctorAdapter]) -> str:
    """Прогнать `scout doctor` без сети, базы и моделей; вернуть напечатанное.

    Живьём доктор ходит в сеть и делает платный вызов модели, поэтому здесь
    подменены ровно три внешние опоры: инициализация БД, роутер моделей и
    реестр источников. Всё остальное — настоящий код `_doctor`.
    """
    import app.db

    class _FakeRouter:
        def __init__(self, settings) -> None:
            self._settings = settings

        async def health(self) -> dict[str, tuple[bool, str]]:
            return {"gigachat": (True, "заглушка")}

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(app.db, "init_db", fake_init_db)
    monkeypatch.setattr("app.search.build_adapters", lambda client, settings, **kwargs: adapters)
    monkeypatch.setattr(cli, "LLMRouter", _FakeRouter)
    recorder = Console(file=io.StringIO(), width=200, record=True)
    monkeypatch.setattr(cli, "console", recorder)

    await cli._doctor()
    return recorder.export_text()


def _doctor_adapters() -> tuple[_DoctorAdapter, _DoctorAdapter, _DoctorAdapter]:
    return (
        _DoctorAdapter("crossref"),
        _DoctorAdapter("rospatent", (STATUS_STUB, "заготовка: источник ещё не подключён")),
        _DoctorAdapter("yandex_search", (STATUS_NO_KEY, "не заполнено: YANDEX_SEARCH_API_KEY")),
    )


async def test_доктор_спрашивает_здоровье_только_у_рабочих_источников(monkeypatch):
    """У `_doctor` не было ни одного автотеста: мутация `working = adapters`
    пережила все 272 теста (M26 аудита), а единственной проверкой половины
    задачи был один платный живой прогон.

    Спрашивать здоровье у заготовки и у источника без ключа — не косметика:
    `health()` рабочего источника ходит в сеть, ответ заранее известен
    («нет ключа»), а у платного источника такая проба стоит денег.
    """
    working, stub, no_key = _doctor_adapters()

    await _run_doctor(monkeypatch, [working, stub, no_key])

    assert working.health_calls == 1, "у рабочего источника здоровье не спрошено"
    assert stub.health_calls == 0, "здоровье спрошено у заготовки"
    assert no_key.health_calls == 0, "здоровье спрошено у источника без ключа"


async def test_доктор_печатает_каждый_источник_с_его_статусом(monkeypatch):
    """РЕШ-52.1 глазами оператора: в таблице обязан быть весь реестр, а не
    только рабочие источники — невидимый источник нельзя ни проверить, ни
    починить. Значений ключей в таблице нет (инвариант №3)."""
    working, stub, no_key = _doctor_adapters()

    text = await _run_doctor(monkeypatch, [working, stub, no_key])

    assert "crossref" in text and "ок, проба выполнена" in text
    assert "rospatent" in text and STATUS_STUB in text
    assert "yandex_search" in text and STATUS_NO_KEY in text
    assert "YANDEX_SEARCH_API_KEY" in text


async def test_доктор_переживает_упавшую_пробу_источника(monkeypatch):
    """`asyncio.gather(..., return_exceptions=True)` — не украшение: источник,
    чья проба падает, обязан стать строкой «ошибка», а не уронить доктора и
    вместе с ним проверку всех остальных источников."""
    working, stub, _ = _doctor_adapters()

    async def broken_health() -> tuple[bool, str]:
        raise RuntimeError("проба не удалась")

    broken = _DoctorAdapter("cyberleninka")
    broken.health = broken_health

    text = await _run_doctor(monkeypatch, [working, broken, stub])

    assert "ошибка" in text and "проба не удалась" in text
    assert working.health_calls == 1, "отказ одного источника отменил проверку остальных"


async def test_здоровье_заготовок_не_падает():
    """Р-13 плана: доктор с `include_stubs=True` не должен спотыкаться о
    заготовку. Ни одна `health()` не поднимает исключение и не ходит в сеть."""
    async with httpx.AsyncClient() as client:
        adapters = build_adapters(client, None, include_stubs=True)
        for adapter in adapters:
            if adapter_status(adapter) is None:
                continue
            ok, detail = await adapter.health()
            assert ok is False and detail


@respx.mock
async def test_здоровье_openalex_проверяется_бесплатным_поиском():
    """Проба доктора обязана трогать ту роль, которая работает всегда.

    Прежняя проба шла в lookup по DOI и без ключа возвращала False. Теперь
    источник включается и без ключа (РЕШ-71), и такая проба докладывала бы «нет
    ключа» про источник, который в этом же прогоне исправно ищет. Состояние
    обогащения при этом не скрывается — оно уходит второй половиной строки.
    """
    route = respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, json=_search_payload()))

    async with httpx.AsyncClient() as client:
        ok, detail = await _adapter(client).health()
        assert ok is True and "кандидатов в пробе: 1" in detail
        assert "OPENALEX_API_KEY" not in detail, "ключ есть, жаловаться не на что"

        no_key_ok, no_key_detail = await _adapter(client, api_key=None).health()

    assert no_key_ok is True, "поиск ключа не требует, проба обязана проходить"
    assert "OPENALEX_API_KEY" in no_key_detail, "выключенное обогащение обязано быть видно доктору"
    assert route.call_count == 2


@respx.mock
async def test_здоровье_отличает_отказ_источника_от_пустой_пробы():
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(503, json={"error": "нет"}))

    async with httpx.AsyncClient() as client:
        ok, detail = await _adapter(client).health()

    assert ok is False and "openalex" in detail


def test_таблица_ключей_доктора_знает_про_роспатент_и_patentsview():
    """РЕШ-52.2: одиннадцать строк, включая два новых ключа."""
    names = [env_name for env_name, _, _ in SECRET_CHECKS]

    assert len(names) == 11
    assert "ROSPATENT_API_KEY" in names and "PATENTSVIEW_API_KEY" in names
    assert len(set(names)) == 11, "дубли в таблице ключей"


# ── Правки пост-аудита (Т-18) ───────────────────────────────────────────────
@respx.mock
async def test_ключ_обогащения_уходит_в_заголовок_запроса():
    """П-1 аудита: шов «ключ → обогащение» не был закреплён ничем.

    Существующая проверка смотрела только, что ключ доехал до КОНСТРУКТОРА
    (`._api_key == "k"`), а не что он уходит В ЗАПРОС. Мутация «удалить
    заголовок `Authorization` из `_lookup`» переживала весь файл: обогащение
    молча ходило бы анонимно, и заметить это было бы нечем до живого прогона.
    """
    route = respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))

    async with httpx.AsyncClient() as client:
        await _adapter(client, api_key="test-key").enrich_by_doi([DOI])

    assert route.calls[0].request.headers.get("Authorization") == "Bearer test-key"


def test_платные_источники_реестра_согласованы_с_required_settings():
    """П-2 аудита: гейт платных источников держится на согласии двух словарей.

    В `build_adapters` источник выключается только тогда, когда `requires_key`
    И непустой список настроек в `REQUIRED_SETTINGS` совпали. Нет записи в
    `REQUIRED_SETTINGS` → `missing == []` → источник включается НЕЗАВИСИМО от
    `requires_key`; именно поэтому мутация «вернуть `requires_key = True` у
    OpenAlex» переживала весь набор. Согласие пары — не косметика: рассогласование
    в одну сторону включает платный источник без ключа, в другую — выключает
    бесплатный.
    """
    paid = {name for name, cls in ADAPTERS.items() if cls.requires_key}

    assert set(REQUIRED_SETTINGS) == paid, (
        "рассогласование REQUIRED_SETTINGS и requires_key: "
        f"без настроек {sorted(paid - set(REQUIRED_SETTINGS))}, "
        f"лишние записи {sorted(set(REQUIRED_SETTINGS) - paid)}"
    )
    assert "openalex" not in paid, "поиск OpenAlex ключа не требует (РЕШ-71)"


def test_горизонт_свежести_закреплён_числом():
    """П-5 аудита: горизонт свежести проверялся выводом ожидаемой даты ИЗ САМОЙ
    константы — самоссылка, проверяющая формат, а не решение. Мутация
    `FRESHNESS_YEARS = 100` переживала весь набор, а прогон тихо начинал тащить
    работы столетней давности."""
    assert FRESHNESS_YEARS == 5
    assert OpenAlexAdapter._from_publication_date() == f"{date.today().year - 5}-01-01"


@respx.mock
async def test_поисковые_запросы_считаются_и_остаток_нормы_запоминается():
    """П-4 (решение владельца): поиск ключа не требует, но тарифицируется.

    Живая анонимная проба вернула `X-RateLimit-Cost-USD: 0.001` при норме
    `X-RateLimit-Limit-USD: 0.1` — сто запросов в сутки. Без счётчика прогон
    незаметно выедает норму: в `Report.cost_rub` эти доллары не попадают (там
    рубли моделей), и больше их не видит никто.
    """
    respx.get(url__startswith=API_URL).mock(
        return_value=httpx.Response(
            200,
            json=_search_payload(),
            headers={"X-RateLimit-Remaining-USD": "0.099", "X-RateLimit-Remaining": "99"},
        )
    )

    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.search(_query(), limit=5)
        await adapter.search(_query(), limit=5)

    assert adapter.search_queries_used == 2
    assert adapter.search_cost_usd == pytest.approx(2 * USD_PER_SEARCH)
    assert adapter.daily_remaining_usd == pytest.approx(0.099)
    assert adapter.daily_remaining_calls == 99


@respx.mock
async def test_отказ_поиска_тоже_попадает_в_счётчик():
    """Источник тарифицирует принятый запрос, а не удачный: 429 и 500 стоят
    ровно столько же. Счётчик, растущий только на успехе, занижал бы расход
    именно в тот прогон, когда источник и так капризничает."""
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(429, json={"error": "слишком часто"}))

    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)

    assert adapter.search_queries_used == 1


@respx.mock
async def test_остаток_нормы_без_заголовков_остаётся_неизвестным():
    """Заголовков может не быть — прокси срезал, источник поменял схему.
    Подставлять сюда расчётное значение нельзя: норма общая на все прогоны за
    сутки, и вычитание своих запросов врало бы после второго запуска."""
    respx.get(url__startswith=API_URL).mock(return_value=httpx.Response(200, json=_search_payload()))

    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.search(_query(), limit=5)

    assert adapter.daily_remaining_usd is None
    assert "остаток источник не сообщил" in adapter.search_usage_note()


async def test_строка_счётчика_называет_платность_норму_и_остаток():
    """Требование П-4 к формулировке: из строки обязано быть понятно, что
    источник ПЛАТНЫЙ, что у него есть суточная норма и сколько её осталось.
    Счётчик без оговорки про платность читается как объём работы — ровно так и
    выглядел бесплатный lookup обогащения."""
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        adapter.search_queries_used = 12
        adapter.daily_remaining_usd = 0.088
        adapter.daily_remaining_calls = 88

        note = adapter.search_usage_note()

    assert "платный" in note.lower(), "строка не говорит, что источник платный"
    assert "суточная" in note and f"${FREE_DAILY_USD:.2f}" in note, "не названа суточная норма"
    assert "осталось $0.088" in note and "88 запросов" in note, "не показан остаток нормы"
    assert "12" in note and "$0.012" in note, "не показан расход этого прогона"


class _CountingPipeline:
    """Пайплайн-заглушка для `cli._run`: в источники и модели не ходит, но
    отдаёт наружу тот же список адаптеров, что и настоящий прогон."""

    adapters: ClassVar[list] = []

    def __init__(self, **kwargs) -> None:
        del kwargs

    async def run(self, brief, **kwargs) -> Report:
        return Report(
            request_id=0,
            brief=brief,
            cards=[],
            summary=ReportSummary(recommendation="тест: пайплайн подменён заглушкой"),
            generated_at=datetime.now(UTC),
        )

    async def aclose(self) -> None:
        pass


async def _run_cli(monkeypatch, settings, fake_provider_factory, tmp_path, adapters: list) -> str:
    """Прогнать `scout run` с подменённым пайплайном; вернуть напечатанное.

    Ни сети, ни базы, ни моделей: подменены интейк, инициализация БД, роутер и
    сам пайплайн. Настоящий здесь только конец `_run` — тот, что печатает
    расход.
    """

    async def fake_init_db() -> None:
        return None

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(
            questions=[],
            draft_brief=Brief(industry="нефтепереработка", task_summary="Катализатор теряет активность."),
        )

    def fake_router(_settings):
        return LLMRouter(settings=settings, providers={"gigachat": fake_provider_factory("gigachat")})

    _CountingPipeline.adapters = adapters
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("app.db.init_db", fake_init_db)
    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr(cli, "LLMRouter", fake_router)
    monkeypatch.setattr(pipeline_pkg, "ScoutPipeline", _CountingPipeline)
    recorder = Console(file=io.StringIO(), width=200, record=True)
    monkeypatch.setattr(cli, "console", recorder)

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", False)
    return recorder.export_text()


async def test_счётчик_поиска_виден_оператору_в_конце_прогона(monkeypatch, settings, fake_provider_factory, tmp_path):
    """Вторая половина П-4: счётчик, которого никто не печатает, ничем не
    отличается от отсутствующего. Строка обязана стоять рядом со стоимостью
    запроса — там, где оператор и смотрит расход."""
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        adapter.search_queries_used = 3
        adapter.daily_remaining_usd = 0.097
        adapter.daily_remaining_calls = 97

        text = await _run_cli(monkeypatch, settings, fake_provider_factory, tmp_path, [adapter])

    assert "OpenAlex" in text and "платный поиск" in text, "расход платного поиска оператору не показан"
    assert "запросов 3" in text and "осталось $0.097" in text


async def test_источник_без_счётчика_ничего_не_печатает(monkeypatch, settings, fake_provider_factory, tmp_path):
    """Обратная сторона утиной проверки: источник, который считать себя не
    умеет, не обязан выдумывать строку и не роняет конец прогона."""
    async with httpx.AsyncClient() as client:
        text = await _run_cli(monkeypatch, settings, fake_provider_factory, tmp_path, [_OneHitAdapter(client)])

    assert "Платные источники" not in text


async def test_пайплайн_отдаёт_свои_источники_наружу(settings):
    """Счётчику нужен путь от адаптера до CLI. До Т-18 его не было вовсе:
    список источников жил приватным полем, и добраться до него можно было
    только через `_adapters`."""
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        pipeline = ScoutPipeline(settings=settings, router=LLMRouter(settings), adapters=[adapter])

        assert pipeline.adapters == [adapter]
        assert ScoutPipeline(settings=settings, router=LLMRouter(settings)).adapters == []


def test_таблица_ключей_доктора_честна_про_закрытый_доступ_к_google():
    """Т-7: пояснение к ключам Google говорит правду, а не рекламирует закрытую дверь.

    Прежнее «100 запросов в сутки бесплатно» читалось как рабочая возможность,
    которой просто не воспользовались. На деле Custom Search JSON API закрыт для
    новых клиентов и отключается 01.01.2027 (РЕШ-21), значения в `.env` очищены
    (РЕШ-32): оператор мог часами добывать ключ, который отдаёт 403.
    """
    purposes = {env_name: purpose for env_name, _, purpose in SECRET_CHECKS}

    for env_name in ("GOOGLE_API_KEY", "GOOGLE_CSE_ID"):
        assert "не используется" in purposes[env_name], f"{env_name}: пояснение не говорит, что источник выключен"
        assert "бесплатно" not in purposes[env_name], f"{env_name}: пояснение всё ещё рекламирует закрытый доступ"
    assert "01.01.2027" in purposes["GOOGLE_API_KEY"], "не назван срок отключения API (РЕШ-21)"
