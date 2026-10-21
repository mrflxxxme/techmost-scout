"""Э6-2в: Яндекс-поиск аналогов по белому списку доменов.

Проверяется весь путь: справочник `reference/analog_sources.yaml` → разбиение
доменов на пачки по пределу длины запроса → платные запросы адаптера в пределах
шести на прогон → видимость пропусков в `limit_problems`. Сети нет: запуск поиска
и опрос операции подменяются через respx, тело запроса перехватывается.
"""

from __future__ import annotations

import base64
import json
import re

import httpx
import pytest
import respx
import yaml

from app.schemas import SearchQuery, SourceType
from app.search import analog_sources
from app.search.analog_sources import (
    ANALOG_QUERY_MAX_CHARS,
    analog_domains,
    load_analog_sources,
    plan_analog_queries,
)
from app.search.base import SearchError
from app.search.yandex_parse import with_site_filter
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter

TEXT_EN = "industrial catalyst regeneration case studies market"
TEXT_RU = "регенерация катализатора кейсы рынок"


def _query(text: str = TEXT_EN, *, lang: str = "en", source_type: SourceType = SourceType.ANALOG) -> SearchQuery:
    return SearchQuery(text=text, source_type=source_type, lang=lang, rationale="тест")


def _xml(*urls: str) -> str:
    docs = "".join(
        f"<doc><url>{url}</url><domain>example.org</domain><title>Кейс {i}</title>"
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


def _mock(xml: str) -> respx.Route:
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(200, json=_done(xml)))
    return route


def _adapter(client: httpx.AsyncClient, **kwargs) -> YandexSearchAdapter:
    params = {"api_key": "test-key", "folder_id": "b1gtest", "poll_interval_seconds": 0.0, "max_wait_seconds": 5.0}
    params.update(kwargs)
    adapter = YandexSearchAdapter(client, **params)
    adapter.min_interval_seconds = 0  # пауза между платными запросами к подменённому ответу не нужна
    return adapter


def _sent_texts(route: respx.Route) -> list[str]:
    return [json.loads(call.request.content)["query"]["queryText"] for call in route.calls]


def _sites(text: str) -> list[str]:
    return re.findall(r"site:([^\s|)]+)", text)


# --- справочник --------------------------------------------------------------------


def test_справочник_читается_и_укладывается_в_20_30_доменов():
    loaded = load_analog_sources()
    domains = analog_domains()
    assert 20 <= len(domains) <= 30
    assert len(set(domains)) == len(domains)
    assert loaded["checked_at"] == "2026-10-21"
    assert len(loaded["reports"]) >= 20


def test_каталоги_ycombinator_и_producthunt_в_списке():
    catalogs = {entry.domain for entry in load_analog_sources()["catalogs"]}
    assert {"ycombinator.com", "producthunt.com"} <= catalogs
    assert {"ycombinator.com", "producthunt.com"} <= set(analog_domains())


def test_виды_отчётных_доменов_заполнены():
    kinds = {entry.kind for entry in load_analog_sources()["reports"]}
    assert kinds == {"bank", "fund", "consultant"}


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


def _yaml(
    reports: str = '  - {domain: "a.com", kind: "bank"}', catalogs: str = '  - {domain: "b.com", kind: "catalog"}'
):
    return f'checked_at: "2026-10-21"\nreports:\n{reports}\ncatalogs:\n{catalogs}\n'


def test_контрольный_справочник_из_подмены_читается(custom_reference):
    custom_reference(_yaml())
    assert analog_domains() == ("b.com", "a.com")


@pytest.mark.parametrize(
    "bad",
    [
        "https://a.com",
        "a.com/reports",
        "a.com/",
        "A.com",
        "a .com",
        "a.com:8080",
        "localhost",
        "-a.com",
    ],
)
def test_домен_со_схемой_путём_или_пробелом_это_ошибка_загрузки(custom_reference, bad):
    custom_reference(_yaml(reports=f'  - {{domain: "{bad}", kind: "bank"}}'))
    with pytest.raises(ValueError, match="Белый список аналогов"):
        load_analog_sources()


def test_дубль_домена_внутри_секции_это_ошибка_загрузки(custom_reference):
    custom_reference(_yaml(reports='  - {domain: "a.com", kind: "bank"}\n  - {domain: "a.com", kind: "fund"}'))
    with pytest.raises(ValueError, match="повторяется"):
        load_analog_sources()


def test_дубль_домена_между_секциями_это_ошибка_загрузки(custom_reference):
    custom_reference(_yaml(catalogs='  - {domain: "a.com", kind: "catalog"}'))
    with pytest.raises(ValueError, match="повторяется"):
        load_analog_sources()


def test_нестроковый_домен_это_ошибка_загрузки(custom_reference):
    custom_reference(_yaml(reports='  - {domain: 123, kind: "bank"}'))
    with pytest.raises(ValueError, match="Белый список аналогов"):
        load_analog_sources()


def test_справочник_читается_только_безопасным_загрузчиком(custom_reference):
    """Инвариант №10: тег объекта Python в файле не исполняется, а отвергается."""
    custom_reference(_yaml(reports='  - {domain: !!python/name:os.getcwd "", kind: "bank"}'))
    with pytest.raises(yaml.YAMLError):
        load_analog_sources()


def test_без_вида_и_без_даты_проверки_это_ошибка_загрузки(custom_reference):
    custom_reference(_yaml(reports='  - {domain: "a.com"}'))
    with pytest.raises(ValueError, match="вид"):
        load_analog_sources()
    custom_reference(_yaml().replace('checked_at: "2026-10-21"', "checked_at: 2026-10-21"))
    with pytest.raises(ValueError, match="checked_at"):
        load_analog_sources()


def test_чужие_секции_файла_не_мешают(custom_reference):
    custom_reference(_yaml() + 'ru_registries:\n  - {domain: "gisp.gov.ru", name_ru: "ГИСП"}\n')
    assert analog_domains() == ("b.com", "a.com")


# --- пачки и сужение ---------------------------------------------------------------


def test_каждая_пачка_не_длиннее_предела_и_все_домены_размещены():
    plan = plan_analog_queries(TEXT_EN)
    assert len(plan.texts) == len(plan.batches) >= 2, (
        "27 доменов в 400 символов не помещаются — пачек должно быть несколько"
    )
    assert all(len(text) <= ANALOG_QUERY_MAX_CHARS for text in plan.texts)
    placed = [domain for batch in plan.batches for domain in batch]
    assert placed == list(analog_domains()), "порядок опроса: каталоги первыми, ни один домен не потерян и не задвоен"
    assert plan.unfit == ()
    for text, batch in zip(plan.texts, plan.batches, strict=True):
        assert _sites(text) == list(batch)


def test_каталоги_попадают_в_первую_пачку():
    first = plan_analog_queries(TEXT_EN).batches[0]
    assert first[:2] == ("ycombinator.com", "producthunt.com")


def test_пачки_заполняются_плотно():
    """Пачка не обрывается раньше времени: следующий домен в неё действительно не влез бы."""
    plan = plan_analog_queries(TEXT_EN)
    for text, following in zip(plan.texts, plan.batches[1:], strict=False):
        assert len(analog_sources.narrow_text(TEXT_EN, (*_sites(text), following[0]))) > ANALOG_QUERY_MAX_CHARS


def test_длинная_формулировка_даёт_пачек_больше_а_не_превышение():
    long_text = "x" * 250
    plan = plan_analog_queries(long_text)
    assert all(len(text) <= ANALOG_QUERY_MAX_CHARS for text in plan.texts)
    assert len(plan.texts) > len(plan_analog_queries(TEXT_EN).texts)


def test_формулировка_без_места_под_site_называет_все_домены_не_влезшими():
    plan = plan_analog_queries("y" * 399)
    assert plan.texts == ()
    assert plan.unfit == analog_domains()


@pytest.mark.parametrize(("text", "lang"), [(TEXT_EN, "en"), (TEXT_RU, "ru")])
def test_with_site_filter_сужает_analog_в_любом_контуре(text, lang):
    narrowed = with_site_filter(_query(text, lang=lang), "SEARCH_TYPE_COM" if lang == "en" else "SEARCH_TYPE_RU")
    assert narrowed.startswith(text)
    assert "ycombinator.com" in _sites(narrowed)
    assert len(narrowed) <= ANALOG_QUERY_MAX_CHARS


def test_with_site_filter_analog_слишком_длинной_формулировки_это_ошибка():
    with pytest.raises(ValueError, match="слишком длинна"):
        with_site_filter(_query("z" * 399), "SEARCH_TYPE_COM")


def test_сужение_по_списку_аналогов_не_задевает_другие_типы():
    """Англоязычная `web` и русская `web` без грантового намерения по-прежнему идут без `site:`."""
    assert with_site_filter(_query(TEXT_EN, source_type=SourceType.WEB), "SEARCH_TYPE_COM") == TEXT_EN
    assert with_site_filter(_query(TEXT_RU, lang="ru", source_type=SourceType.WEB), "SEARCH_TYPE_RU") == TEXT_RU
    grant = with_site_filter(_query(TEXT_RU, lang="ru", source_type=SourceType.GRANT), "SEARCH_TYPE_RU")
    assert _sites(grant) == ["rscf.ru", "fasie.ru"], "сужение грантов прежнее"


# --- адаптер -----------------------------------------------------------------------


@respx.mock
async def test_analog_уходит_пачками_каждая_с_site_и_в_пределе_длины():
    route = _mock(_xml("https://www.mckinsey.com/industries/a", "https://a16z.com/b"))
    plan = plan_analog_queries(TEXT_EN)
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        found, raw = await adapter.search_raw(_query(), limit=5)

    texts = _sent_texts(route)
    assert len(texts) == len(plan.texts), "каждая пачка доменов — один платный запрос"
    assert adapter.queries_used == len(plan.texts)
    assert texts == list(plan.texts)
    assert all(len(text) <= ANALOG_QUERY_MAX_CHARS and "site:" in text for text in texts)
    assert sorted(site for text in texts for site in _sites(text)) == sorted(analog_domains())
    assert adapter.limit_problems == []
    assert found and all(candidate.source_type is SourceType.ANALOG for candidate in found)
    assert raw, "сырые записи нужны стадии extractor"


@respx.mock
async def test_кандидаты_разных_пачек_не_задваиваются():
    _mock(_xml("https://www.mckinsey.com/industries/a", "https://a16z.com/b"))
    async with httpx.AsyncClient() as client:
        found = await _adapter(client).search(_query(), limit=5)
    assert [str(c.url) for c in found] == ["https://www.mckinsey.com/industries/a", "https://a16z.com/b"]


@respx.mock
async def test_русская_формулировка_analog_идёт_в_российский_контур_тоже_суженной():
    route = _mock(_xml("https://www.bcg.com/x"))
    async with httpx.AsyncClient() as client:
        await _adapter(client).search(_query(TEXT_RU, lang="ru"), limit=5)
    bodies = [json.loads(call.request.content)["query"] for call in route.calls]
    assert {body["searchType"] for body in bodies} == {"SEARCH_TYPE_RU"}
    assert all("site:" in body["queryText"] for body in bodies)


@respx.mock
async def test_пачки_сверх_предела_называются_в_limit_problems_поимённо():
    route = _mock(_xml("https://www.bain.com/x"))
    plan = plan_analog_queries(TEXT_EN)
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=1)
        found = await adapter.search(_query(), limit=5)

    assert len(route.calls) == 1, "второй платный запрос сверх предела уйти не должен"
    assert adapter.queries_used == 1
    assert found, "оплаченная пачка отдала кандидатов"
    problems = adapter.limit_problems
    assert len(problems) == 1
    assert "предел 1" in problems[0] and "yandex_search" in problems[0] and TEXT_EN in problems[0]
    skipped = [domain for batch in plan.batches[1:] for domain in batch]
    assert skipped
    for domain in skipped:
        assert domain in problems[0], f"не влезший домен {domain} не назван"
    for domain in plan.batches[0]:
        assert domain not in problems[0], f"опрошенный домен {domain} назван пропущенным"


@respx.mock
async def test_исчерпанный_предел_до_первой_пачки_называет_все_домены():
    route = _mock(_xml("https://www.bain.com/x"))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=2)
        adapter.queries_used = 2
        found = await adapter.search(_query(), limit=5)

    assert found == []
    assert len(route.calls) == 0
    (problem,) = adapter.limit_problems
    assert all(domain in problem for domain in analog_domains())


@respx.mock
async def test_формулировка_без_места_под_site_не_уходит_а_домены_названы():
    route = _mock(_xml("https://www.bain.com/x"))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        found = await adapter.search(_query("w" * 399), limit=5)

    assert found == [] and len(route.calls) == 0
    (problem,) = adapter.limit_problems
    assert "слишком длинна" in problem and "ycombinator.com" in problem


@respx.mock
async def test_отказ_источника_на_пачке_поднимается_как_ошибка_поиска():
    """Инвариант №5: HTTP-ошибка — отказ, а не пустой успех."""
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        with pytest.raises(SearchError, match="yandex_search"):
            await adapter.search(_query(), limit=5)


@respx.mock
async def test_пустая_выдача_пачек_это_пустой_список_без_проблем():
    _mock(_xml())
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        assert await adapter.search(_query(), limit=5) == []
    assert adapter.limit_problems == []


@respx.mock
async def test_без_ключа_analog_не_уходит_никуда():
    route = _mock(_xml("https://www.bain.com/x"))
    async with httpx.AsyncClient() as client:
        adapter = YandexSearchAdapter(client)
        assert await adapter.search_raw(_query(), limit=5) == ([], [])
    assert len(route.calls) == 0


@respx.mock
async def test_прочие_типы_формулировок_уходят_одним_запросом_как_раньше():
    route = _mock(_xml("https://www.bain.com/x"))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.search(_query(TEXT_EN, source_type=SourceType.WEB), limit=5)
    assert _sent_texts(route) == [TEXT_EN]
    assert adapter.queries_used == 1
