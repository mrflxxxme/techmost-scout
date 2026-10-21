"""Адаптер Wikidata (Э6-2б): компании и продукты как зарубежные аналоги.

Сети нет (инвариант №7): ответ SPARQL-сервиса подменён respx, фикстуры урезаны
по образцу живого ответа. Отказ источника — `SearchError` (инвариант №5),
успешный пустой ответ — пустой список.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search import build_adapters
from app.search.base import SearchError
from app.search.wikidata import ANALOG_CLASSES, SPARQL_URL, WikidataAdapter


def _query(text: str = "cold chain monitoring", lang: str = "en") -> SearchQuery:
    return SearchQuery(text=text, source_type=SourceType.ANALOG, lang=lang, rationale="тест")


def _cell(value: str, kind: str = "literal") -> dict:
    return {"type": kind, "value": value}


def _row(entity: str = "Q7692685", label: str | None = "Techstars", **extra: str) -> dict:
    """Одна строка ответа: на сущность их может быть несколько (страна × сайт × отрасль)."""
    row = {"item": _cell(f"http://www.wikidata.org/entity/{entity}", "uri")}
    if label is not None:
        row["itemLabel"] = _cell(label)
    for key, value in extra.items():
        row[key] = _cell(value, "uri" if key == "site" else "literal")
    return row


def _payload(*rows: dict) -> dict:
    return {"head": {"vars": []}, "results": {"bindings": list(rows)}}


_TECHSTARS = _row(
    itemDescription="U.S. startup accelerator",
    inception="2006-01-01T00:00:00Z",
    site="http://techstars.com",
    cc="US",
    industryLabel="venture capital",
)


async def _search(payload: dict, query: SearchQuery | None = None, **kwargs):
    async with httpx.AsyncClient() as client:
        adapter = WikidataAdapter(client, **kwargs)
        with respx.mock:
            route = respx.get(SPARQL_URL).mock(return_value=httpx.Response(200, json=payload))
            found = await adapter.search(query or _query(), limit=20)
    return found, route


def test_контракт_адаптера():
    assert WikidataAdapter.source_types == (SourceType.ANALOG,)
    assert WikidataAdapter.requires_key is False
    assert WikidataAdapter.needs_extraction is False
    assert WikidataAdapter.name == "wikidata"


async def test_разбор_в_кандидата_с_полями_контракта():
    found, _route = await _search(_payload(_TECHSTARS))

    assert len(found) == 1
    candidate = found[0]
    assert candidate.source_type is SourceType.ANALOG
    assert candidate.provider == "wikidata"
    assert candidate.title == "Techstars"
    assert str(candidate.url) == "https://www.wikidata.org/wiki/Q7692685"
    assert candidate.raw_id == "Q7692685"
    assert candidate.year == 2006
    assert candidate.country == "US"
    for part in ("страна: US", "основана: 2006", "сайт: http://techstars.com", "отрасль: venture capital"):
        assert part in candidate.snippet


async def test_строки_одной_сущности_склеиваются_в_одного_кандидата():
    """Сайт × отрасль даёт несколько строк на сущность: кандидат один, значения собраны."""
    rows = [
        _row(itemDescription="d", cc="US", site="http://a.example", industryLabel="software"),
        _row(itemDescription="d", cc="US", site="http://b.example", industryLabel="hardware"),
        _row("Q42", "Другая", cc="DE"),
    ]
    found, _route = await _search(_payload(*rows))

    assert [c.raw_id for c in found] == ["Q7692685", "Q42"]
    assert "software, hardware" in found[0].snippet
    assert found[0].country == "US"
    assert found[1].country == "DE"


async def test_две_страны_p17_дают_country_none():
    rows = [_row(cc="US"), _row(cc="CA")]
    found, _route = await _search(_payload(*rows))

    assert len(found) == 1
    assert found[0].country is None
    assert "страна: US, CA" in found[0].snippet


async def test_одна_страна_повторённая_строками_остаётся_страной():
    found, _route = await _search(
        _payload(_row(cc="US", site="http://a.example"), _row(cc="US", site="http://b.example"))
    )

    assert found[0].country == "US"


async def test_нет_страны_нет_года_нет_сайта_кандидат_всё_равно_есть():
    found, _route = await _search(_payload(_row()))

    assert found[0].country is None
    assert found[0].year is None
    assert found[0].snippet == ""


async def test_год_до_нашей_эры_не_берётся():
    found, _route = await _search(_payload(_row(inception="-0500-01-01T00:00:00Z")))

    assert found[0].year is None


async def test_сущность_без_названия_отброшена():
    """Сервис подписывает безымянную сущность её номером — это не название."""
    found, _route = await _search(_payload(_row("Q51827066", "Q51827066"), _row("Q9", None), _TECHSTARS))

    assert [c.raw_id for c in found] == ["Q7692685"]


async def test_предел_limit_соблюдён():
    rows = [_row(f"Q{n}", f"Компания {n}") for n in range(1, 8)]
    async with httpx.AsyncClient() as client, respx.mock:
        respx.get(SPARQL_URL).mock(return_value=httpx.Response(200, json=_payload(*rows)))
        found = await WikidataAdapter(client).search(_query(), limit=3)

    assert len(found) == 3


@pytest.mark.parametrize("status", [500, 429, 503])
async def test_http_ошибка_поднимает_searcherror(status: int):
    async with httpx.AsyncClient() as client, respx.mock:
        respx.get(SPARQL_URL).mock(return_value=httpx.Response(status, text="сбой"))
        with pytest.raises(SearchError) as caught:
            await WikidataAdapter(client).search(_query(), limit=5)

    assert str(status) in str(caught.value)
    assert "SELECT" not in str(caught.value), "в сообщение об отказе не должен попадать текст запроса"


@pytest.mark.parametrize("error", [httpx.ConnectError("обрыв"), httpx.ReadTimeout("таймаут")])
async def test_обрыв_соединения_поднимает_searcherror(error: Exception):
    async with httpx.AsyncClient() as client, respx.mock:
        respx.get(SPARQL_URL).mock(side_effect=error)
        with pytest.raises(SearchError):
            await WikidataAdapter(client).search(_query(), limit=5)


async def test_не_json_поднимает_searcherror():
    async with httpx.AsyncClient() as client, respx.mock:
        respx.get(SPARQL_URL).mock(return_value=httpx.Response(200, text="<html>Service unavailable</html>"))
        with pytest.raises(SearchError):
            await WikidataAdapter(client).search(_query(), limit=5)


@pytest.mark.parametrize("body", [[], {"results": {}}, {"results": {"bindings": "нет"}}, "строка"])
async def test_json_без_results_bindings_поднимает_searcherror(body):
    async with httpx.AsyncClient() as client, respx.mock:
        respx.get(SPARQL_URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(SearchError):
            await WikidataAdapter(client).search(_query(), limit=5)


async def test_пустой_успех_это_пустой_список_без_исключения():
    found, _route = await _search(_payload())

    assert found == []


async def test_пустая_выдача_длинной_формулировки_повторяется_один_раз_укороченной():
    text = "cold chain temperature monitoring platform for pharmaceutical distribution"
    found, route = await _search(_payload(), _query(text))

    assert found == []
    assert route.call_count == 2
    first, second = (call.request.url.params["query"] for call in route.calls)
    assert text in first
    assert "cold chain temperature" in second
    assert text not in second


async def test_короткая_формулировка_без_повтора():
    _found, route = await _search(_payload(), _query("Techstars"))

    assert route.call_count == 1


async def test_повтор_при_отказе_тоже_searcherror():
    """Пустой первый ответ, отказ на повторе: источник недоступен, а не «ничего»."""
    text = "cold chain temperature monitoring platform"
    async with httpx.AsyncClient() as client, respx.mock:
        respx.get(SPARQL_URL).mock(side_effect=[httpx.Response(200, json=_payload()), httpx.Response(429)])
        with pytest.raises(SearchError):
            await WikidataAdapter(client).search(_query(text), limit=5)


async def test_запрос_json_формата_и_только_компании_и_продукты():
    _found, route = await _search(_payload(_TECHSTARS))

    request = route.calls.last.request
    assert request.url.params["format"] == "json"
    sparql = request.url.params["query"]
    for cls in ANALOG_CLASSES:
        assert f"wd:{cls}" in sparql
    assert "wdt:P31" in sparql
    assert "P17" in sparql
    assert "P297" in sparql
    assert 'mwapi:search "cold chain monitoring"' in sparql


async def test_кавычка_в_формулировке_не_выходит_из_строкового_литерала():
    _found, route = await _search(_payload(_TECHSTARS), _query('acme" } DROP \\ "x'))

    sparql = route.calls.last.request.url.params["query"]
    line = next(line for line in sparql.splitlines() if "mwapi:search" in line)
    assert line.count('"') == 2, line
    assert "\\" not in line


async def test_русская_формулировка_ищется_по_русским_названиям():
    _found, route = await _search(_payload(), _query("холодовая цепь", lang="ru"))

    sparql = route.calls[0].request.url.params["query"]
    assert 'mwapi:language "ru"' in sparql


async def test_user_agent_несёт_контакт_из_настройки():
    _found, route = await _search(_payload(_TECHSTARS), contact_email="owner@example.org")

    assert "mailto:owner@example.org" in route.calls.last.request.headers["user-agent"]


async def test_build_adapters_включает_wikidata_без_ключей_и_передаёт_контакт():
    class _Settings:
        crossref_mailto = "owner@example.org"

    async with httpx.AsyncClient() as client:
        adapters = {a.name: a for a in build_adapters(client, _Settings())}

    adapter = adapters["wikidata"]
    assert isinstance(adapter, WikidataAdapter)
    assert getattr(adapter, "disabled_status", None) is None
    assert "owner@example.org" in adapter.user_agent
    async with httpx.AsyncClient() as client:
        assert "wikidata" in {a.name for a in build_adapters(client, None)}
