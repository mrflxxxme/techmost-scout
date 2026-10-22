"""Э6-8: реестры РФ как источники аналогов (РЕШ-261, РЕШ-277, эскалация Э-4 — вариант А).

Проба доступа (`.planning/evidence/Э6-W4-Э6-8-probe.txt`) показала: ГИСП и реестр
Минцифры закрыты, у Product Radar нет машинного поиска продуктов без ключа. Поэтому
реестры опрашивает Яндекс по домену: отдельный список `ru_registries` сужает только
русскую формулировку `analog`, которую при «Рынке» добавляет код. Сети в тестах нет —
ответы Яндекса подменены через respx.
"""

from __future__ import annotations

import base64
import json
import logging
import re

import httpx
import pytest
import respx

from app.llm import LLMRouter
from app.llm.base import LLMResult, Usage
from app.pipeline import planning
from app.pipeline.orchestrator import ScoutPipeline, split_analog_candidates
from app.schemas import Brief, ScoredCandidate, SearchPlan, SearchQuery, SourceType
from app.search import analog_sources
from app.search.analog_sources import (
    ANALOG_QUERY_MAX_CHARS,
    analog_domains,
    load_analog_sources,
    plan_analog_queries,
)
from app.search.yandex_parse import with_site_filter
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter

#: Реестры РЕШ-261, которые не Яндекс: ГИСП, реестр ПО Минцифры, Product Radar.
REGISTRY_DOMAINS = {"gisp.gov.ru", "reestr.digital.gov.ru", "productradar.ru"}

TEXT_RU = "система контроля качества сварных швов"
TEXT_EN = "weld quality monitoring system case studies"


# ── Помощники ────────────────────────────────────────────────────────────────
def _q(text: str, source_type: SourceType, lang: str = "ru") -> SearchQuery:
    return SearchQuery(text=text, source_type=source_type, lang=lang, rationale="тест")


def _market(brief: Brief, kind: str = "product") -> Brief:
    return brief.model_copy(update={"solution_kind": kind})


def _model_plan(count: int = 4, *, analog: int = 2, web_ru: bool = True) -> SearchPlan:
    """План «от модели»: публикации, одна русская web (по желанию) и английские analog."""
    queries = [_q(f"публикация {n}", SourceType.PUBLICATION) for n in range(count)]
    if web_ru:
        queries.append(_q(TEXT_RU, SourceType.WEB))
    queries.append(_q("weld monitoring", SourceType.WEB, "en"))
    queries.extend(_q(f"{TEXT_EN} {n}", SourceType.ANALOG, "en") for n in range(analog))
    return SearchPlan(queries=queries)


def _analog(plan: SearchPlan) -> list[SearchQuery]:
    return [q for q in plan.queries if q.source_type is SourceType.ANALOG]


def _sites(text: str) -> list[str]:
    return re.findall(r"site:([^\s|)]+)", text)


def _xml(*urls: str) -> str:
    docs = "".join(
        f"<doc><url>{url}</url><domain>example.org</domain><title>Запись {i}</title>"
        f"<passages><passage>Фрагмент {i}.</passage></passages></doc>"
        for i, url in enumerate(urls)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<yandexsearch version='1.0'><response><results><grouping><group>{docs}"
        "</group></grouping></results></response></yandexsearch>"
    )


def _done(xml: str) -> dict:
    return {"done": True, "response": {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}}


def _adapter(client: httpx.AsyncClient, **kwargs) -> YandexSearchAdapter:
    params = {"api_key": "test-key", "folder_id": "b1gtest", "poll_interval_seconds": 0.0, "max_wait_seconds": 5.0}
    params.update(kwargs)
    adapter = YandexSearchAdapter(client, **params)
    adapter.min_interval_seconds = 0
    return adapter


def _mock_by_text(registry_xml: str, other_xml: str) -> respx.Route:
    """Яндекс отвечает записью реестра только на запрос, суженный до реестра Минцифры."""
    sent: dict[str, str] = {}

    def start(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["query"]["queryText"]
        op_id = f"op-{len(sent)}"
        sent[op_id] = registry_xml if "site:reestr.digital.gov.ru" in text else other_xml
        return httpx.Response(200, json={"id": op_id})

    def poll(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_done(sent[request.url.path.rsplit("/", 1)[-1]]))

    route = respx.post(SEARCH_ASYNC_URL).mock(side_effect=start)
    respx.get(url__startswith=OPERATION_URL).mock(side_effect=poll)
    return route


def _sent_queries(route: respx.Route) -> list[dict]:
    return [json.loads(call.request.content)["query"] for call in route.calls]


@pytest.fixture
def custom_reference(tmp_path, monkeypatch):
    """Подменить файл справочника своим текстом; кэш загрузчика сбрасывается до и после."""

    def put(text: str) -> None:
        path = tmp_path / "analog_sources.yaml"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(analog_sources, "_REFERENCE_PATH", path)
        load_analog_sources.cache_clear()

    yield put
    load_analog_sources.cache_clear()


def _yaml(registries: str) -> str:
    return (
        'checked_at: "2026-10-21"\n'
        'reports:\n  - {domain: "a.com", kind: "bank"}\n'
        'catalogs:\n  - {domain: "b.com", kind: "catalog"}\n'
        f"ru_registries:\n{registries}\n"
    )


# ── Список ru_registries ─────────────────────────────────────────────────────
def test_ru_registries_ровно_три_домена_решения_261():
    registries = load_analog_sources()["ru_registries"]

    assert {entry.domain for entry in registries} == REGISTRY_DOMAINS
    assert len(registries) == 3
    assert all(entry.name_ru.strip() for entry in registries)


def test_каждый_реестр_решения_261_покрыт_списком_и_ни_один_не_в_мировом():
    """Адаптера к реестрам нет (проба), покрытие — только `ru_registries`; мировой список их не содержит."""
    assert set(analog_domains("ru")) == REGISTRY_DOMAINS
    assert not REGISTRY_DOMAINS & set(analog_domains("en"))
    assert not REGISTRY_DOMAINS & set(analog_domains())


def test_мировые_списки_отчётов_и_каталогов_не_менялись():
    loaded = load_analog_sources()

    assert [entry.domain for entry in loaded["catalogs"]] == ["ycombinator.com", "producthunt.com"]
    assert len(loaded["reports"]) == 25
    assert len(analog_domains()) == 27


@pytest.mark.parametrize("bad", ["https://gisp.gov.ru", "gisp.gov.ru/reestr", "GISP.gov.ru", "gisp .gov.ru", "gisp"])
def test_домен_реестра_со_схемой_путём_или_пробелом_это_ошибка_загрузки(custom_reference, bad):
    custom_reference(_yaml(f'  - {{domain: "{bad}", name_ru: "Реестр"}}'))
    with pytest.raises(ValueError, match="Белый список аналогов"):
        load_analog_sources()


def test_дубль_домена_реестра_это_ошибка_загрузки(custom_reference):
    custom_reference(_yaml('  - {domain: "x.ru", name_ru: "Один"}\n  - {domain: "x.ru", name_ru: "Два"}'))
    with pytest.raises(ValueError, match="повторяется"):
        load_analog_sources()


def test_реестр_с_доменом_мирового_списка_это_ошибка_загрузки(custom_reference):
    custom_reference(_yaml('  - {domain: "a.com", name_ru: "Двойной"}'))
    with pytest.raises(ValueError, match="повторяется"):
        load_analog_sources()


def test_реестр_без_названия_и_пустая_секция_это_ошибка_загрузки(custom_reference):
    custom_reference(_yaml('  - {domain: "x.ru"}'))
    with pytest.raises(ValueError, match="name_ru"):
        load_analog_sources()
    custom_reference(_yaml("  []"))
    with pytest.raises(ValueError, match="ru_registries"):
        load_analog_sources()


def test_нестроковый_домен_реестра_это_ошибка_загрузки(custom_reference):
    custom_reference(_yaml('  - {domain: 123, name_ru: "Число"}'))
    with pytest.raises(ValueError, match="Белый список аналогов"):
        load_analog_sources()


def test_читается_подменный_справочник_с_реестром(custom_reference):
    custom_reference(_yaml('  - {domain: "x.ru", name_ru: "Свой реестр"}'))
    assert analog_domains("ru") == ("x.ru",)
    assert analog_domains("en") == ("b.com", "a.com")


# ── Сужение: русская analog — реестры, английская — мир ──────────────────────
def test_русская_analog_несёт_site_только_реестров():
    narrowed = with_site_filter(_q(TEXT_RU, SourceType.ANALOG, "ru"), "SEARCH_TYPE_RU")

    assert narrowed.startswith(TEXT_RU)
    assert sorted(_sites(narrowed)) == sorted(REGISTRY_DOMAINS)
    assert len(narrowed) <= ANALOG_QUERY_MAX_CHARS
    assert len(plan_analog_queries(TEXT_RU, lang="ru").texts) == 1, "три реестра — одна платная пачка"


def test_английская_analog_не_несёт_ни_одного_домена_реестра():
    plan = plan_analog_queries(TEXT_EN, lang="en")

    assert plan.texts
    for text in plan.texts:
        assert not REGISTRY_DOMAINS & set(_sites(text))
    assert "ycombinator.com" in _sites(with_site_filter(_q(TEXT_EN, SourceType.ANALOG, "en"), "SEARCH_TYPE_COM"))


# ── registry_analog_query ────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["product", "digital", "business_model"])
def test_рынок_даёт_одну_русскую_analog_с_текстом_русской_web(brief, kind):
    query = planning.registry_analog_query(_model_plan(), _market(brief, kind))

    assert query is not None
    assert (query.source_type, query.lang, query.text) == (SourceType.ANALOG, "ru", TEXT_RU)
    assert query.rationale


def test_берётся_первая_русская_web_а_не_английская_и_не_иного_типа(brief):
    plan = SearchPlan(
        queries=[
            _q("english web", SourceType.WEB, "en"),
            _q("русская публикация", SourceType.PUBLICATION),
            _q("первая русская web", SourceType.WEB),
            _q("вторая русская web", SourceType.WEB),
        ]
    )

    assert planning.registry_analog_query(plan, _market(brief)).text == "первая русская web"


def test_без_русской_web_текст_из_первых_трёх_keywords_ru(brief):
    market = _market(brief).model_copy(update={"keywords_ru": ["один", "два", "три", "четыре"]})

    query = planning.registry_analog_query(_model_plan(web_ru=False), market)

    assert query is not None and query.text == "один два три"


def test_без_русской_web_и_без_keywords_ru_формулировки_нет_и_есть_строка_в_журнале(brief, caplog):
    market = _market(brief).model_copy(update={"keywords_ru": []})

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        assert planning.registry_analog_query(_model_plan(web_ru=False), market) is None

    assert "registry_analog_query" in caplog.text


def test_research_русской_формулировки_не_строит(brief):
    assert brief.solution_kind == "research"
    assert planning.registry_analog_query(_model_plan(), brief) is None


# ── Нормализация плана ───────────────────────────────────────────────────────
def test_normalize_добавляет_русскую_analog_к_двум_английским(brief):
    result = planning.normalize_plan_queries(_model_plan(), _market(brief))

    analog = _analog(result)
    assert [q.lang for q in analog].count("en") == planning.ANALOG_QUERIES == 2
    assert [q.lang for q in analog].count("ru") == 1
    assert next(q for q in analog if q.lang == "ru").text == TEXT_RU


def test_normalize_русская_analog_от_модели_не_принимается_и_заменяется_кодовой(brief):
    plan = _model_plan(analog=1)
    plan = plan.model_copy(
        update={"queries": [*plan.queries, _q("произвольный русский аналог", SourceType.ANALOG, "ru")]}
    )

    result = planning.normalize_plan_queries(plan, _market(brief))

    ru = [q for q in _analog(result) if q.lang == "ru"]
    assert [q.text for q in ru] == [TEXT_RU]
    assert [q.lang for q in _analog(result)].count("en") == 1


def test_normalize_research_не_даёт_ни_одной_analog(brief):
    assert _analog(planning.normalize_plan_queries(_model_plan(), brief)) == []


def test_план_10_плюс_2_плюс_1_после_нормализации_не_длиннее_потолка(brief):
    """10 формулировок основной очереди + 2 английские analog от модели: русская и две конкурсные не помещаются."""
    plan = _model_plan(count=8, analog=2)
    assert len(plan.queries) == 10 + 2

    result = planning.normalize_plan_queries(plan, _market(brief))

    assert len(result.queries) <= 12
    assert len(_analog(result)) == 3, "аналоги в хвосте не вытеснены, вытесняется основная очередь"
    assert sum(q.source_type is SourceType.CONTEST for q in result.queries) == 2
    SearchPlan.model_validate(result.model_dump())  # план читается той же схемой, что и при записи


def test_второй_проход_русской_analog_не_несёт(brief):
    result = planning.normalize_plan_queries(_model_plan(), _market(brief))
    assert any(q.lang == "ru" for q in _analog(result)), "условие теста: в первом плане русская есть"

    assert SourceType.ANALOG not in [q.source_type for q in planning.strip_analog_queries(result).queries]


# ── Адаптер: что уходит в Яндекс и что видно при пределе ─────────────────────
@respx.mock
async def test_яндекс_получает_русскую_analog_с_site_реестров_и_английскую_без_них():
    route = _mock_by_text(_xml("https://reestr.digital.gov.ru/reestr/1/"), _xml("https://www.mckinsey.com/a"))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.search(_q(TEXT_RU, SourceType.ANALOG, "ru"), limit=5)
        used_for_ru = adapter.queries_used
        await adapter.search(_q(TEXT_EN, SourceType.ANALOG, "en"), limit=5)

    sent = _sent_queries(route)
    ru_calls, en_calls = sent[:used_for_ru], sent[used_for_ru:]
    assert [q["searchType"] for q in ru_calls] == ["SEARCH_TYPE_RU"] and used_for_ru == 1
    assert sorted(_sites(ru_calls[0]["queryText"])) == sorted(REGISTRY_DOMAINS)
    assert en_calls and all(q["searchType"] == "SEARCH_TYPE_COM" for q in en_calls)
    for call in en_calls:
        assert not REGISTRY_DOMAINS & set(_sites(call["queryText"]))


@respx.mock
async def test_исчерпанный_предел_называет_домены_реестров_в_limit_problems():
    route = _mock_by_text(_xml("https://reestr.digital.gov.ru/reestr/1/"), _xml())
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=2)
        adapter.queries_used = 2
        found = await adapter.search(_q(TEXT_RU, SourceType.ANALOG, "ru"), limit=5)

    assert found == [] and len(route.calls) == 0
    (problem,) = adapter.limit_problems
    assert "yandex_search" in problem and TEXT_RU in problem
    for domain in REGISTRY_DOMAINS:
        assert domain in problem


@respx.mock
async def test_отказ_яндекса_на_реестровой_формулировке_поднимается_как_search_error():
    from app.search.base import SearchError

    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(500, text="сбой"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await _adapter(client).search(_q(TEXT_RU, SourceType.ANALOG, "ru"), limit=5)


# ── Сквозной: бриф → план → Яндекс → кандидат ANALOG → аналоговая часть ─────
class _GarbageProvider:
    """Стадия extractor отвечает не-JSON: сработает откат на разбор адаптера (запись реестра не теряется)."""

    name = "deepseek"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    async def complete(self, messages, *, model=None, temperature=0.2, max_tokens=4096, json_mode=False):
        return LLMResult(
            text="не JSON",
            usage=Usage(tokens_in=1, tokens_out=1, cost_rub=0.001, provider=self.name, model="fake"),
        )

    async def health(self):
        return True, "заглушка"


@respx.mock
async def test_продукт_запись_реестра_доходит_до_аналоговой_части(settings, brief):
    market = _market(brief, "product")
    # Только формулировки `analog`: предел платных запросов делят все типы, и учёт предела
    # проверяют тесты Э6-2в; здесь важен путь реестровой формулировки до аналоговой части.
    full = planning.normalize_plan_queries(_model_plan(), market)
    plan = SearchPlan(queries=_analog(full))
    registry_url = "https://reestr.digital.gov.ru/reestr/1234567/"
    route = _mock_by_text(_xml(registry_url), _xml())
    router = LLMRouter(settings, providers={"deepseek": _GarbageProvider()})

    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        outcome = await ScoutPipeline(settings=settings, router=router, adapters=[adapter]).gather_candidates(plan)

    ru_texts = [q["queryText"] for q in _sent_queries(route) if q["searchType"] == "SEARCH_TYPE_RU"]
    assert any(text.startswith(TEXT_RU) and "site:reestr.digital.gov.ru" in text for text in ru_texts)

    registry = [c for c in outcome.candidates if str(c.url) == registry_url]
    assert registry and all(c.source_type is SourceType.ANALOG for c in registry), "запись реестра — кандидат ANALOG"
    scored = [
        ScoredCandidate(candidate=c, relevance=6, maturity=6, accessibility=6, freshness=6, completeness=6)
        for c in outcome.candidates
    ]
    _, analogs = split_analog_candidates(scored)
    assert registry_url in {str(item.candidate.url) for item in analogs}
    assert outcome.unavailable == []
