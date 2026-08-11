"""Адаптер Google: разбор выдачи, постраничный обход, отказы, включение.

Сети здесь нет: ответы Google подменяются через respx. Проверяем ровно то, что
может сломаться молча — склейку дублей, счётчик платных запросов и поведение
при исчерпанной квоте.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search import build_adapters
from app.search.base import normalize_url
from app.search.google_search import API_URL, GoogleSearchAdapter


def _item(title: str, link: str, *, snippet: str = "фрагмент", display: str = "example.org", year: str | None = None):
    item: dict = {"title": title, "link": link, "snippet": snippet, "displayLink": display}
    if year:
        item["pagemap"] = {"metatags": [{"article:published_time": f"{year}-05-01T10:00:00Z"}]}
    return item


def _query(text: str = "hydrotreating catalyst deactivation") -> SearchQuery:
    return SearchQuery(text=text, source_type=SourceType.WEB, lang="en", rationale="тест")


@pytest.fixture
def adapter_factory():
    def make(client: httpx.AsyncClient, **kwargs) -> GoogleSearchAdapter:
        params = {"api_key": "test-key", "cse_id": "test-cx", "fx_rate": 100.0}
        params.update(kwargs)
        return GoogleSearchAdapter(client, **params)

    return make


@respx.mock
async def test_разбирает_выдачу_в_кандидатов(adapter_factory):
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    _item(
                        "Catalyst deactivation study",
                        "https://www.mit.edu/research/catalyst/",
                        display="www.mit.edu",
                        year="2024",
                    )
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        found = await adapter_factory(client).search(_query(), limit=5)

    assert len(found) == 1
    card = found[0]
    assert card.source_type is SourceType.WEB
    assert card.provider == "google_search"
    assert card.title == "Catalyst deactivation study"
    # Домен — догадка о принадлежности разработки
    assert card.org == "www.mit.edu"
    assert card.year == 2024
    # Ключ склейки — нормализованный адрес без схемы, www и хвостового слэша
    assert card.raw_id == "mit.edu/research/catalyst"


@respx.mock
async def test_страницы_запрашиваются_пока_не_наберётся_лимит(adapter_factory):
    """limit=15 — это две платные страницы: десять ссылок и ещё пять."""
    first = [_item(f"Работа {i}", f"https://a{i}.example.org/page") for i in range(10)]
    second = [_item(f"Работа 1{i}", f"https://b{i}.example.org/page") for i in range(5)]
    route = respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(200, json={"items": first}),
            httpx.Response(200, json={"items": second}),
        ]
    )
    async with httpx.AsyncClient() as client:
        adapter = adapter_factory(client)
        found = await adapter.search(_query(), limit=15)

    assert len(found) == 15
    assert route.call_count == 2
    assert adapter.queries_used == 2
    # Вторая страница просит ровно недостающие пять, начиная с одиннадцатой
    assert dict(route.calls[1].request.url.params)["start"] == "11"
    assert dict(route.calls[1].request.url.params)["num"] == "5"
    # Стоимость считается по числу запросов, а не по числу ссылок
    assert adapter.estimated_cost_rub == pytest.approx(2 * 0.005 * 100.0)


@respx.mock
async def test_короткая_выдача_обрывает_обход(adapter_factory):
    """Если Google вернул меньше запрошенного, второй страницы уже нет."""
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(200, json={"items": [_item("Одна", "https://x.example.org/a")]})
    )
    async with httpx.AsyncClient() as client:
        adapter = adapter_factory(client)
        found = await adapter.search(_query(), limit=20)

    assert len(found) == 1
    assert route.call_count == 1
    assert adapter.queries_used == 1


@respx.mock
async def test_дубли_по_адресу_склеиваются(adapter_factory):
    """Один и тот же адрес в разном написании — один кандидат."""
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    _item("Страница", "https://www.example.org/tech/"),
                    _item("Страница под другим названием", "https://example.org/tech"),
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        found = await adapter_factory(client).search(_query(), limit=10)

    assert len(found) == 1


@pytest.mark.parametrize("status", [403, 429])
@respx.mock
async def test_исчерпанная_квота_не_роняет_пайплайн(adapter_factory, status):
    respx.get(API_URL).mock(
        return_value=httpx.Response(status, json={"error": {"code": status, "message": "dailyLimitExceeded"}})
    )
    async with httpx.AsyncClient() as client:
        adapter = adapter_factory(client)
        found = await adapter.search(_query(), limit=10)

    assert found == []
    # Отказ по квоте всё равно был обращением к API — считаем его
    assert adapter.queries_used == 1


@respx.mock
async def test_доктор_различает_отказ_доступа_и_пустую_выдачу(adapter_factory):
    """Отказ и пустота — разные болезни, и доктор обязан их различать.

    До этого health() на 403 сообщал «ключ принят, но выдача пуста» и отправлял
    чинить настройку поисковой системы вместо доступа к API.
    """
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            403, json={"error": {"code": 403, "message": "This project does not have the access"}}
        )
    )
    async with httpx.AsyncClient() as client:
        ok, detail = await adapter_factory(client).health()

    assert ok is False
    assert "403" in detail
    assert "does not have the access" in detail
    assert "выдача пуста" not in detail

    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"items": []}))
    async with httpx.AsyncClient() as client:
        ok, detail = await adapter_factory(client).health()

    assert ok is False
    assert "выдача пуста" in detail


@respx.mock
async def test_запись_без_ссылки_отбрасывается(adapter_factory):
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"title": "Без адреса"}, _item("С адресом", "https://ok.example.org/p")]},
        )
    )
    async with httpx.AsyncClient() as client:
        found = await adapter_factory(client).search(_query(), limit=10)

    assert [c.title for c in found] == ["С адресом"]


@respx.mock
async def test_язык_не_ограничивается_по_умолчанию(adapter_factory):
    """Смысл источника — мировой охват, поэтому lr не передаётся."""
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, json={"items": []}))
    async with httpx.AsyncClient() as client:
        await adapter_factory(client).search(_query(), limit=5)
        assert "lr" not in dict(route.calls[0].request.url.params)

    route.reset()
    async with httpx.AsyncClient() as client:
        await adapter_factory(client, lang_restrict="lang_en").search(_query(), limit=5)
        assert dict(route.calls[0].request.url.params)["lr"] == "lang_en"


async def test_без_ключей_поиск_пуст_и_здоровье_отрицательно():
    async with httpx.AsyncClient() as client:
        adapter = GoogleSearchAdapter(client)
        assert await adapter.search(_query(), limit=5) == []
        ok, detail = await adapter.health()
    assert ok is False
    assert "GOOGLE_API_KEY" in detail


async def test_источник_включается_только_с_обоими_настройками():
    async with httpx.AsyncClient() as client:
        names = {a.name for a in build_adapters(client, {"google_api_key": "k"})}
        assert "google_search" not in names, "без GOOGLE_CSE_ID источник искать негде"

        names = {a.name for a in build_adapters(client, {"google_api_key": "k", "google_cse_id": "cx"})}
        assert "google_search" in names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Example.org/Tech/", "example.org/tech"),
        ("http://example.org/tech", "example.org/tech"),
        ("https://example.org/tech?utm_source=x", "example.org/tech"),
        ("https://example.org", "example.org"),
    ],
)
def test_нормализация_адреса(raw, expected):
    assert normalize_url(raw) == expected
