"""Адаптер Яндекса: два контура, предел трат, разбор XML, отказы.

Сети нет: и запуск поиска, и опрос операции подменяются через respx. XML взят
по образцу фактического ответа Яндекса, снятого живым вызовом 11 августа 2026, —
включая подсветку слов в <hlword> и время правки страницы в modtime.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search import build_adapters
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter


def _xml(*docs: str) -> str:
    body = "".join(docs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<yandexsearch version='1.0'><response><results><grouping><group>{body}"
        "</group></grouping></results></response></yandexsearch>"
    )


def _doc(url: str, title: str = "Заголовок", *, domain: str = "example.org", modtime: str = "20240115T103000") -> str:
    return (
        "<doc>"
        f"<url>{url}</url><domain>{domain}</domain>"
        f"<title>{title} про <hlword>катализатор</hlword></title>"
        f"<modtime>{modtime}</modtime>"
        "<passages><passage>Первый фрагмент.</passage><passage>Второй фрагмент.</passage></passages>"
        "</doc>"
    )


def _operation_done(xml: str) -> dict:
    return {"done": True, "response": {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}}


def _query(text: str = "катализатор", lang: str = "ru", source_type: SourceType = SourceType.WEB) -> SearchQuery:
    return SearchQuery(text=text, source_type=source_type, lang=lang, rationale="тест")


@pytest.fixture
def make_adapter():
    def build(client: httpx.AsyncClient, **kwargs) -> YandexSearchAdapter:
        params = {
            "api_key": "test-key",
            "folder_id": "b1gtest",
            # В тестах ждать нечего: операция готова сразу
            "poll_interval_seconds": 0.0,
            "max_wait_seconds": 5.0,
        }
        params.update(kwargs)
        return YandexSearchAdapter(client, **params)

    return build


def _mock_ok(xml: str):
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(200, json=_operation_done(xml)))


@respx.mock
async def test_разбирает_ответ_в_кандидатов(make_adapter):
    _mock_ok(_xml(_doc("https://pure.manchester.ac.uk/lab/", "Лаборатория", domain="pure.manchester.ac.uk")))
    async with httpx.AsyncClient() as client:
        found = await make_adapter(client).search(_query(), limit=10)

    assert len(found) == 1
    card = found[0]
    assert card.source_type is SourceType.WEB
    assert card.provider == "yandex_search"
    # Подсветка <hlword> склеивается обратно в связный заголовок
    assert card.title == "Лаборатория про катализатор"
    assert card.org == "pure.manchester.ac.uk"
    assert card.year == 2024
    assert card.snippet == "Первый фрагмент. Второй фрагмент."
    # Ключ склейки общий с другими веб-источниками
    assert card.raw_id == "pure.manchester.ac.uk/lab"


@pytest.mark.parametrize(
    ("lang", "expected_type"),
    [("ru", "SEARCH_TYPE_RU"), ("en", "SEARCH_TYPE_COM")],
)
@respx.mock
async def test_контур_выбирается_по_языку_формулировки(make_adapter, lang, expected_type):
    """Русский запрос идёт в российскую выдачу, английский — в мировую."""
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(_doc("https://a.example.org/p"))))
    )
    async with httpx.AsyncClient() as client:
        await make_adapter(client).search(_query(lang=lang), limit=5)

    sent = json.loads(route.calls[0].request.content)
    assert sent["query"]["searchType"] == expected_type


@pytest.mark.parametrize(
    ("lang", "expected_label"),
    [("ru", "yandex-ru"), ("en", "yandex-com")],
)
async def test_метка_контура_совпадает_с_выбором_по_языку(make_adapter, lang, expected_label):
    """Метка для hits_by_provider берётся тем же правилом, что и сам контур —
    без живого вызова: contour_label не ходит в сеть."""
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client)
        assert adapter.contour_label(_query(lang=lang)) == expected_label


@respx.mock
async def test_предел_платных_запросов_на_прогон(make_adapter):
    """Седьмая формулировка уже не уходит в сеть — запросы стоят денег."""
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(_doc("https://a.example.org/p"))))
    )
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client, max_queries=6)
        for _ in range(8):
            await adapter.search(_query(), limit=5)

    assert route.call_count == 6
    assert adapter.queries_used == 6


@respx.mock
async def test_грантовый_маркер_в_web_формулировке_сужает_до_реестров_только_в_ru(make_adapter):
    """Сужение `site:` больше не зависит от типа `grant` (планировщик его не выдаёт, РЕШ-39) —
    оно включается по маркеру грантового намерения в тексте русской web-формулировки (РЕШ-41)."""
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(_doc("https://rscf.ru/project/1"))))
    )
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client)
        await adapter.search(
            _query(text="поддержанные проекты РНФ катализатор гидроочистки", lang="ru", source_type=SourceType.WEB),
            limit=5,
        )
        ru_grant_text = json.loads(route.calls[0].request.content)["query"]["queryText"]

        await adapter.search(
            _query(text="supported grant projects catalyst research", lang="en", source_type=SourceType.WEB),
            limit=5,
        )
        en_text = json.loads(route.calls[1].request.content)["query"]["queryText"]

        await adapter.search(
            _query(text="лаборатория катализа сайт кафедры", lang="ru", source_type=SourceType.WEB), limit=5
        )
        ru_plain_text = json.loads(route.calls[2].request.content)["query"]["queryText"]

        # Совет ревью Ф-3: «конкурс» — тоже маркер грантового намерения (правило 9 planner.md
        # предлагает его модели), а без других маркеров формулировка со словом «конкурс» не должна
        # оставаться без сужения.
        await adapter.search(
            _query(text="конкурс поддержки разработок катализатор гидроочистки", lang="ru", source_type=SourceType.WEB),
            limit=5,
        )
        ru_konkurs_text = json.loads(route.calls[3].request.content)["query"]["queryText"]

    assert "site:rscf.ru" in ru_grant_text and "site:fasie.ru" in ru_grant_text
    # В мировом контуре российские реестры бессмысленны, даже если в тексте есть слово grant
    assert "site:" not in en_text
    # Обычная русская web-формулировка без грантового маркера — без сужения
    assert "site:" not in ru_plain_text
    assert "site:rscf.ru" in ru_konkurs_text and "site:fasie.ru" in ru_konkurs_text


@respx.mock
async def test_дубли_по_адресу_склеиваются(make_adapter):
    _mock_ok(
        _xml(
            _doc("https://www.example.org/tech/", "Страница"),
            _doc("https://example.org/tech", "Она же под другим названием"),
        )
    )
    async with httpx.AsyncClient() as client:
        found = await make_adapter(client).search(_query(), limit=10)

    assert len(found) == 1


@respx.mock
async def test_отказ_внутри_xml_не_роняет_пайплайн(make_adapter):
    """Яндекс сообщает об отказе внутри XML, а не кодом HTTP."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><yandexsearch><response>'
        '<error code="55">Превышен лимит запросов</error>'
        "</response></yandexsearch>"
    )
    _mock_ok(xml)
    async with httpx.AsyncClient() as client:
        found = await make_adapter(client).search(_query(), limit=10)

    assert found == []


@respx.mock
async def test_незавершившаяся_операция_возвращает_пусто(make_adapter):
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(200, json={"done": False}))
    async with httpx.AsyncClient() as client:
        found = await make_adapter(client, max_wait_seconds=0.2).search(_query(), limit=5)

    assert found == []


@respx.mock
async def test_ошибка_операции_возвращает_пусто(make_adapter):
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json={"done": True, "error": {"message": "permission denied"}})
    )
    async with httpx.AsyncClient() as client:
        found = await make_adapter(client).search(_query(), limit=5)

    assert found == []


async def test_без_ключей_поиск_пуст_и_здоровье_отрицательно():
    async with httpx.AsyncClient() as client:
        adapter = YandexSearchAdapter(client)
        assert await adapter.search(_query(), limit=5) == []
        ok, detail = await adapter.health()
    assert ok is False
    assert "YANDEX_SEARCH_API_KEY" in detail


async def test_источник_включается_только_с_обоими_настройками():
    async with httpx.AsyncClient() as client:
        names = {a.name for a in build_adapters(client, {"yandex_search_api_key": "k"})}
        assert "yandex_search" not in names, "без каталога запрос отправлять некуда"

        names = {a.name for a in build_adapters(client, {"yandex_search_api_key": "k", "yandex_catalog_id": "b1g"})}
        assert "yandex_search" in names, "источник больше не заготовка и должен собираться"


@respx.mock
async def test_страница_из_двух_поисковиков_склеивается_в_одного_кандидата(make_adapter):
    """Главная причина общего ключа склейки: один адрес — один кандидат."""
    from app.search.google_search import API_URL, GoogleSearchAdapter

    _mock_ok(_xml(_doc("https://pure.manchester.ac.uk/lab/", "Лаборатория")))
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "title": "Lab page",
                        "link": "https://www.pure.manchester.ac.uk/lab",
                        "snippet": "…",
                        "displayLink": "pure.manchester.ac.uk",
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        from_yandex = await make_adapter(client).search(_query(), limit=5)
        google = GoogleSearchAdapter(client, api_key="k", cse_id="cx")
        from_google = await google.search(_query(lang="en"), limit=5)

    assert from_yandex[0].raw_id == from_google[0].raw_id
    assert from_yandex[0].dedup_key() == from_google[0].dedup_key()
