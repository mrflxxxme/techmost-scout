"""Адаптер каталога Y Combinator (Э6-2б): стартапы как зарубежные аналоги.

Сети нет (инвариант №7): файл каталога подменён respx, фикстура урезана по
образцу живого `all.json` (≈10,6 МБ, 6320 записей на 21.10.2026).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search import build_adapters
from app.search.base import SearchError
from app.search.yc_catalog import CATALOG_URL, YCCatalogAdapter, _country


def _query(text: str = "cold chain monitoring", lang: str = "en") -> SearchQuery:
    return SearchQuery(text=text, source_type=SourceType.ANALOG, lang=lang, rationale="тест")


def _company(name: str, **fields) -> dict:
    slug = name.lower().replace(" ", "-")
    record = {
        "id": 1,
        "name": name,
        "slug": slug,
        "website": f"https://{slug}.example",
        "all_locations": "San Francisco, CA, USA",
        "long_description": "",
        "one_liner": "",
        "industry": "B2B",
        "subindustry": "B2B -> Supply Chain and Logistics",
        "tags": [],
        "industries": ["B2B"],
        "batch": "Winter 2021",
        "status": "Active",
        "url": f"https://www.ycombinator.com/companies/{slug}",
    }
    record.update(fields)
    return record


_COLD = _company(
    "Coldline",
    one_liner="Cold chain monitoring for vaccines",
    long_description="Sensors and software that track temperature along the cold chain.",
    tags=["Logistics", "IoT"],
    all_locations="Boston, MA, USA",
    batch="Summer 2019",
)
_NOISE = _company("Poultry Pal", one_liner="Chicken farm software")
_CATALOG = [_NOISE, _COLD]


async def _search(catalog, query: SearchQuery | None = None, *, limit: int = 20):
    async with httpx.AsyncClient() as client, respx.mock:
        route = respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog))
        found = await YCCatalogAdapter(client).search(query or _query(), limit=limit)
    return found, route


def test_контракт_адаптера():
    assert YCCatalogAdapter.source_types == (SourceType.ANALOG,)
    assert YCCatalogAdapter.requires_key is False
    assert YCCatalogAdapter.needs_extraction is False
    assert YCCatalogAdapter.name == "yc_catalog"


async def test_разбор_в_кандидата_с_полями_контракта():
    found, _route = await _search(_CATALOG)

    assert len(found) == 1
    candidate = found[0]
    assert candidate.source_type is SourceType.ANALOG
    assert candidate.provider == "yc_catalog"
    assert candidate.title == "Coldline"
    assert candidate.org == "Coldline"
    assert str(candidate.url) == "https://www.ycombinator.com/companies/coldline"
    assert candidate.raw_id == "coldline"
    assert candidate.year == 2019
    assert candidate.country == "US"
    assert "Cold chain monitoring for vaccines" in candidate.snippet
    assert "Summer 2019" in candidate.snippet


async def test_кандидат_не_на_ycombinator_com_отброшен():
    foreign = [
        {**_COLD, "name": "Coldline Mirror", "url": "https://coldline.example/companies/coldline"},
        {**_COLD, "name": "Coldline Fake", "url": "https://ycombinator.com.evil.example/companies/x"},
        {**_COLD, "name": "Coldline Ftp", "url": "ftp://www.ycombinator.com/companies/x"},
        {**_COLD, "name": "Coldline Empty", "url": ""},
    ]
    found, _route = await _search([*foreign, _COLD])

    assert [c.title for c in found] == ["Coldline"]


async def test_голый_ycombinator_com_и_поддомен_допустимы():
    bare = {**_COLD, "name": "Coldline Bare", "url": "https://ycombinator.com/companies/coldline-bare"}
    found, _route = await _search([bare])

    assert [str(c.url) for c in found] == ["https://ycombinator.com/companies/coldline-bare"]


async def test_второй_поиск_не_делает_второго_запроса():
    async with httpx.AsyncClient() as client, respx.mock:
        route = respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=_CATALOG))
        adapter = YCCatalogAdapter(client)
        first = await adapter.search(_query("cold chain monitoring"), limit=5)
        second = await adapter.search(_query("poultry farm software"), limit=5)

    assert route.call_count == 1
    assert [c.title for c in first] == ["Coldline"]
    assert [c.title for c in second] == ["Poultry Pal"]


async def test_параллельные_поиски_качают_каталог_один_раз():
    async with httpx.AsyncClient() as client, respx.mock:
        route = respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=_CATALOG))
        adapter = YCCatalogAdapter(client)
        await asyncio.gather(*(adapter.search(_query(), limit=5) for _ in range(4)))

    assert route.call_count == 1


@pytest.mark.parametrize("status", [500, 429])
async def test_http_ошибка_поднимает_searcherror(status: int):
    async with httpx.AsyncClient() as client, respx.mock:
        respx.get(CATALOG_URL).mock(return_value=httpx.Response(status))
        with pytest.raises(SearchError) as caught:
            await YCCatalogAdapter(client).search(_query(), limit=5)

    assert str(status) in str(caught.value)


@pytest.mark.parametrize("error", [httpx.ConnectError("обрыв"), httpx.ReadTimeout("таймаут")])
async def test_обрыв_соединения_поднимает_searcherror(error: Exception):
    async with httpx.AsyncClient() as client, respx.mock:
        respx.get(CATALOG_URL).mock(side_effect=error)
        with pytest.raises(SearchError):
            await YCCatalogAdapter(client).search(_query(), limit=5)


@pytest.mark.parametrize("body", ["<html>Not Found</html>", '{"companies": []}', '"строка"'])
async def test_не_json_и_не_список_поднимают_searcherror(body: str):
    async with httpx.AsyncClient() as client, respx.mock:
        respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, text=body))
        with pytest.raises(SearchError):
            await YCCatalogAdapter(client).search(_query(), limit=5)


async def test_отказ_не_запоминается_следующий_поиск_пробует_снова():
    async with httpx.AsyncClient() as client, respx.mock:
        route = respx.get(CATALOG_URL).mock(side_effect=[httpx.Response(500), httpx.Response(200, json=_CATALOG)])
        adapter = YCCatalogAdapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)
        found = await adapter.search(_query(), limit=5)

    assert route.call_count == 2
    assert [c.title for c in found] == ["Coldline"]


async def test_успешный_пустой_каталог_и_нет_совпадений_это_пустой_список():
    empty, _route = await _search([])
    nothing, _route = await _search(_CATALOG, _query("quantum gravity telescope"))

    assert empty == []
    assert nothing == []


async def test_одно_общее_слово_из_нескольких_не_считается_совпадением():
    """«platform» есть почти у каждого: нужно два слова из формулировки."""
    one_word = _company("Alpha", one_liner="A platform for everything", subindustry="")
    found, _route = await _search([one_word], _query("cold chain platform"))

    assert found == []


async def test_совпадение_по_имени_тегам_отрасли_и_длинному_описанию():
    by_name = _company("Biosensor Labs", one_liner="x")
    by_tags = _company("Alpha", tags=["Biosensor", "Diagnostics"])
    by_industry = _company("Beta", industries=["Biosensor", "Diagnostics"], subindustry="")
    by_long = _company("Gamma", long_description="We build biosensor diagnostics devices.")
    found, _route = await _search([by_name, by_tags, by_industry, by_long], _query("biosensor diagnostics"))

    assert {c.title for c in found} == {"Alpha", "Beta", "Gamma"}


async def test_основа_слова_ловит_словоформы():
    found, _route = await _search(_CATALOG, _query("monitors cold chains"))

    assert [c.title for c in found] == ["Coldline"]


async def test_выдача_упорядочена_по_весу_совпадений_и_ограничена_limit():
    weak = _company("Weak", long_description="cold chain monitoring")
    strong = _company("Strong", one_liner="cold chain monitoring")
    found, _route = await _search([weak, strong], limit=1)

    assert [c.title for c in found] == ["Strong"]


async def test_русская_формулировка_пропускается_без_запроса():
    found, route = await _search(_CATALOG, _query("холодовая цепь", lang="ru"))

    assert found == []
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("locations", "expected"),
    [
        ("San Francisco, CA, USA", "US"),
        ("San Francisco, CA, USA; Remote", "US"),
        ("New York, NY, USA; Austin, TX, USA", "US"),
        ("London, England, United Kingdom", "GB"),
        ("San Francisco, CA, USA; London, England, United Kingdom", None),
        ("Remote", None),
        ("", None),
        ("Atlantis, Nowhere", None),
        ("Berlin, Germany; Atlantis, Nowhere", None),
    ],
)
def test_страна_только_при_однозначном_сопоставлении(locations: str, expected: str | None):
    assert _country(locations) == expected


async def test_страна_кандидата_по_таблице_и_без_неё():
    uk = _company("Coldline UK", one_liner="cold chain monitoring", all_locations="London, England, United Kingdom")
    two = _company("Coldline Two", one_liner="cold chain monitoring", all_locations="Boston, MA, USA; Paris, France")
    found, _route = await _search([uk, two])

    assert {c.title: c.country for c in found} == {"Coldline UK": "GB", "Coldline Two": None}


async def test_запись_без_названия_и_не_словарь_пропущены():
    found, _route = await _search([None, "строка", {"url": "https://www.ycombinator.com/companies/x"}, _COLD])

    assert [c.title for c in found] == ["Coldline"]


async def test_build_adapters_включает_yc_catalog_без_ключей():
    async with httpx.AsyncClient() as client:
        adapters = {a.name: a for a in build_adapters(client, None)}

    assert isinstance(adapters["yc_catalog"], YCCatalogAdapter)
    assert getattr(adapters["yc_catalog"], "disabled_status", None) is None
    assert "wikidata" in adapters
