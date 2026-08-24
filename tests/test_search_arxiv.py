"""arXiv: повтор с обобщением при пустой выдаче (диагноз Э2-Ж, Д-2).

Живые числа диагноза (`docs/sources.md`): семисловная формулировка модели
уходит булевым `AND` всех термов (`PHRASE_WORD_LIMIT` = 6) и почти всегда даёт
`found=0` — семь обязательных слов редко встречаются в одном препринте
одновременно. Тот же приём, что у КиберЛенинки (З-2) и Роспатента (Д-2):
ступенчатое укорачивание внутри адаптера — при `found=0` на исходной
формулировке ОДИН повтор с первыми 3–4 содержательными словами, собранными
уже фразовым запросом. Сети здесь нет (инвариант №7) — respx.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search import arxiv as arxiv_module
from app.search.arxiv import API_URL, ArxivAdapter
from app.search.base import SearchError

#: 7 содержательных слов — за пределом PHRASE_WORD_LIMIT (6), уходит как AND.
_LONG_QUERY = "deep learning ultrasound diagnosis pregnancy detection sows"
#: Первые 4 содержательных слова той же формулировки — уже в пределах
#: PHRASE_WORD_LIMIT, поэтому короткая форма всегда идёт фразовым поиском.
_SHORT_QUERY = "deep learning ultrasound diagnosis"

_LONG_SEARCH_QUERY = " AND ".join(f"all:{word}" for word in _LONG_QUERY.split()[:6])
_SHORT_SEARCH_QUERY = f'ti:"{_SHORT_QUERY}" OR abs:"{_SHORT_QUERY}"'


def _feed(*entries: str) -> str:
    body = "".join(entries)
    return f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'


def _entry(ident: str, title: str = "Deep learning ultrasound diagnosis") -> str:
    return (
        f"<entry><id>http://arxiv.org/abs/{ident}</id><title>{title}</title>"
        "<summary>Обзор методов ультразвуковой диагностики.</summary>"
        "<published>2024-03-01T00:00:00Z</published>"
        "<author><name>Ivan Petrov</name></author></entry>"
    )


def _query(text: str = _LONG_QUERY, lang: str = "en") -> SearchQuery:
    return SearchQuery(text=text, source_type=SourceType.PUBLICATION, lang=lang, rationale="тест")


def _by_search_query(long_response: httpx.Response, short_response: httpx.Response):
    """Разные ответы на длинную (AND) и укороченную (фразовую) форму — по
    параметру `search_query` фактически ушедшего запроса."""

    def side_effect(request: httpx.Request) -> httpx.Response:
        return long_response if request.url.params["search_query"] == _LONG_SEARCH_QUERY else short_response

    return side_effect


# ── Повтор при пустой выдаче ────────────────────────────────────────────────


@respx.mock
async def test_повтор_при_пустой_выдаче_укороченной_формулировкой(caplog):
    """Критерий 1: `found=0` на исходной (AND) формулировке → один повтор
    укороченной (фразовой) формой; обе формы и оба found видны в логе."""
    route = respx.get(API_URL).mock(
        side_effect=_by_search_query(
            httpx.Response(200, text=_feed()),
            httpx.Response(200, text=_feed(_entry("2401.00001"), _entry("2401.00002"))),
        )
    )
    caplog.set_level("INFO", logger=arxiv_module.__name__)
    async with httpx.AsyncClient() as client:
        adapter = ArxivAdapter(client)
        adapter.min_interval_seconds = 0.0
        found = await adapter.search(_query(), limit=5)

    assert len(found) == 2
    assert route.call_count == 2, "должно быть ровно два обращения: исходная форма + один повтор"
    queries = [call.request.url.params["search_query"] for call in route.calls]
    assert queries == [_LONG_SEARCH_QUERY, _SHORT_SEARCH_QUERY]
    assert _LONG_SEARCH_QUERY in caplog.text
    assert _SHORT_SEARCH_QUERY in caplog.text
    assert "found=0" in caplog.text and "found=2" in caplog.text


@respx.mock
async def test_богатая_выдача_повтора_не_даёт():
    """Мутация (б): непустая выдача на исходной формулировке — повтора быть
    не должно, источник получает ровно один запрос."""
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, text=_feed(_entry("2401.00001"))))
    async with httpx.AsyncClient() as client:
        adapter = ArxivAdapter(client)
        adapter.min_interval_seconds = 0.0
        found = await adapter.search(_query(), limit=5)

    assert len(found) == 1
    assert route.call_count == 1, "непустая выдача не должна вызывать повтор"


@respx.mock
async def test_пустая_выдача_на_обеих_формах_честный_пустой_список():
    """Критерий 2: если и укороченная форма ничего не нашла — это результат
    поиска, а не отказ источника."""
    respx.get(API_URL).mock(return_value=httpx.Response(200, text=_feed()))
    async with httpx.AsyncClient() as client:
        adapter = ArxivAdapter(client)
        adapter.min_interval_seconds = 0.0
        found = await adapter.search(_query(), limit=5)

    assert found == []


@respx.mock
async def test_короткая_формулировка_с_пустой_выдачей_не_даёт_повтора():
    """Краевой случай: формулировка уже в пределах `SHORT_QUERY_WORDS` (или
    короткая форма совпадает с исходной) — повторять запрос тем же телом нет
    смысла, источник получает ровно один запрос."""
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, text=_feed()))
    async with httpx.AsyncClient() as client:
        adapter = ArxivAdapter(client)
        adapter.min_interval_seconds = 0.0
        found = await adapter.search(_query("ultrasound diagnosis pregnancy"), limit=5)

    assert found == []
    assert route.call_count == 1, "короткий запрос сам с собой повторяться не должен"


@respx.mock
async def test_отказ_на_повторе_поднимает_searcherror():
    """Мутация (а)/критерий 3: транспортный отказ на повторе поднимается тем
    же контрактом, что и на первой попытке; повтор ровно один, не больше."""
    route = respx.get(API_URL).mock(
        side_effect=_by_search_query(httpx.Response(200, text=_feed()), httpx.Response(500, text="internal error"))
    )
    async with httpx.AsyncClient() as client:
        adapter = ArxivAdapter(client)
        adapter.min_interval_seconds = 0.0
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)

    assert route.call_count == 2


@respx.mock
async def test_повтор_тоже_соблюдает_паузу():
    """Критерий 4: повтор — такой же сетевой вызов и подчиняется тому же
    `min_interval_seconds`, что и исходный запрос (условие публичного API)."""
    respx.get(API_URL).mock(
        side_effect=_by_search_query(
            httpx.Response(200, text=_feed()), httpx.Response(200, text=_feed(_entry("2401.00001")))
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = ArxivAdapter(client)
        assert ArxivAdapter.min_interval_seconds >= 3.0, "лимит публичного API — 1 запрос в 3 секунды"
        adapter.min_interval_seconds = 0.2  # реальные 3 с раздули бы набор тестов
        started = time.perf_counter()
        await adapter.search(_query(), limit=5)
        elapsed = time.perf_counter() - started

    assert elapsed >= adapter.min_interval_seconds * 0.9, f"повтор ушёл без паузы, за {elapsed:.3f} с"


def test_min_interval_seconds_по_умолчанию_не_смягчён():
    """Страж от смягчения темпа при рефакторинге повтора: лимит платформы —
    условие публичного API, а не деталь реализации, которую можно подвинуть."""
    assert ArxivAdapter.min_interval_seconds == 3.0
