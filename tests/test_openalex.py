"""OpenAlex: обогащение по DOI, запрет поиска и полная таблица доктора (Т-3).

Сети здесь нет: ответы OpenAlex подменяются через respx (инвариант №7).
Проверяется ровно то, что может сломаться молча: ритм обхода, отказ, который
обязан быть виден, и правило «непустое поле кандидата не перезаписывается».
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest
import respx

from app.cli import SECRET_CHECKS
from app.llm import LLMRouter
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import Candidate, ReportSummary, SearchPlan, SearchQuery, SourceType
from app.search import ADAPTERS, STATUS_NO_KEY, STATUS_STUB, adapter_status, build_adapters
from app.search.base import SearchAdapter, SearchError
from app.search.openalex import API_URL, BATCH_SIZE, OpenAlexAdapter, normalize_doi

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


# ── Поиск запрещён ──────────────────────────────────────────────────────────
async def test_поиск_через_openalex_запрещён():
    """РЕШ-10: search платный и дублирует бесплатный Crossref."""
    query = SearchQuery(text="катализатор", source_type=SourceType.PUBLICATION, lang="ru", rationale="тест")

    async with httpx.AsyncClient() as client:
        with pytest.raises(NotImplementedError):
            await _adapter(client).search(query, limit=5)


@respx.mock
async def test_источник_обогащения_не_опрашивается_поиском(settings):
    """Источник обогащения не попадает в `sources_unavailable`.

    Без флага `enrich_only` оркестратор спросил бы у него `search`, получил
    `NotImplementedError` и написал бы клиенту «не ответил openalex» — про
    источник, который никто и не спрашивал.
    """
    route = respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))
    plan = SearchPlan(
        queries=[SearchQuery(text="катализатор", source_type=SourceType.PUBLICATION, lang="ru", rationale="тест")]
    )

    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=LLMRouter(settings), adapters=[_adapter(client)])
        outcome = await pipeline.gather_candidates(plan)

    assert outcome.candidates == []
    assert outcome.unavailable == [], "источник обогащения выдан за отказавший поиск"
    assert route.call_count == 0


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
    assert any("отброшено негодных полей — 1" in message for message in messages)


@respx.mock
async def test_кандидат_без_doi_обогащение_не_запрашивает(settings):
    route = respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))
    patent = _candidate(raw_id="RU2799999", url="https://patents.example/RU2799999", source_type=SourceType.PATENT)

    async with httpx.AsyncClient() as client:
        enriched, citations = await _enrich(settings, _adapter(client), [patent])

    assert route.call_count == 0
    assert enriched == [patent] and citations == {}


async def test_отказ_обогащения_не_роняет_прогон(settings, caplog):
    """Адаптер поднимает `SearchError`, оркестратор решает продолжать: отказ
    виден в логе, кандидаты остаются прежними (РЕШ-52.3)."""

    class _FailingEnricher:
        name = "openalex"

        async def enrich_by_doi(self, dois: list[str]) -> dict:
            raise SearchError("openalex не ответил на lookup DOI")

    candidate = _candidate(org=None)

    enriched, citations = await _enrich(settings, _FailingEnricher(), [candidate])

    assert enriched == [candidate] and citations == {}
    assert any("обогащение по DOI не удалось" in record.getMessage() for record in caplog.records)


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
    отсутствующего.
    """
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))
    captured: dict = {}

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


# ── Доктор ──────────────────────────────────────────────────────────────────
async def test_доктор_видит_все_источники_реестра():
    """РЕШ-52.1: `include_stubs=True` показывает все источники — заготовки как
    «не реализовано», источники без ключей как «нет ключа»."""
    async with httpx.AsyncClient() as client:
        adapters = build_adapters(client, None, include_stubs=True)

    assert [a.name for a in adapters] == list(ADAPTERS), "таблица доктора обязана показывать весь реестр"
    statuses = {a.name: adapter_status(a) for a in adapters}
    assert statuses["crossref"] is None, "бесплатный рабочий источник статуса не имеет"
    assert statuses["rospatent"][0] == STATUS_STUB
    assert statuses["patents_view"][0] == STATUS_STUB
    assert statuses["openalex"][0] == STATUS_NO_KEY
    assert "OPENALEX_API_KEY" in statuses["openalex"][1]
    assert "test" not in statuses["openalex"][1], "в пояснении не может быть значений ключей (инвариант №3)"


async def test_openalex_с_ключом_становится_рабочим_источником():
    async with httpx.AsyncClient() as client:
        adapters = build_adapters(client, {"openalex_api_key": "k"}, include_stubs=True)
        working = build_adapters(client, {"openalex_api_key": "k"})

    assert adapter_status(next(a for a in adapters if a.name == "openalex")) is None
    assert "openalex" in {a.name for a in working}, "с ключом источник обязан быть и в рабочем наборе"


async def test_рабочий_набор_прежний_заготовок_в_нём_нет():
    """Пайплайн зовёт `build_adapters` без `include_stubs` — там по-прежнему
    только то, что действительно работает."""
    async with httpx.AsyncClient() as client:
        names = {a.name for a in build_adapters(client, None)}

    assert "rospatent" not in names and "patents_view" not in names
    assert "openalex" not in names, "без ключа обогащение не включается"
    assert "crossref" in names


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
async def test_здоровье_openalex_проверяется_бесплатным_lookup():
    route = respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json=_work()))

    async with httpx.AsyncClient() as client:
        ok, detail = await _adapter(client).health()
        assert ok is True and "lookup" in detail

        no_key_ok, no_key_detail = await _adapter(client, api_key=None).health()

    assert no_key_ok is False and "OPENALEX_API_KEY" in no_key_detail
    assert route.call_count == 1, "проба без ключа не должна ходить в сеть"


def test_таблица_ключей_доктора_знает_про_роспатент_и_patentsview():
    """РЕШ-52.2: одиннадцать строк, включая два новых ключа."""
    names = [env_name for env_name, _, _ in SECRET_CHECKS]

    assert len(names) == 11
    assert "ROSPATENT_API_KEY" in names and "PATENTSVIEW_API_KEY" in names
    assert len(set(names)) == 11, "дубли в таблице ключей"
