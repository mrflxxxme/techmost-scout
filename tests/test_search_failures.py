"""Отказ источников доходит до вызывающего кода, а не гасится в [] (Ф-5, РЕШ-45).

`arxiv`, `semanticscholar`, `cyberleninka` до правки Ф-5 ловили транспортный
отказ (HTTP-ошибка, 429, нечитаемый ответ) внутри адаптера и превращали его в
обычный пустой список — неотличимый от честного «источник ответил, но ничего
не нашёл». Из-за этого ветка `unavailable` в оркестраторе была недостижима:
`_safe_search` мог поймать только исключение, а адаптер его не поднимал.

Пост-аудит фазы (РЕШ-45) нашёл тот же дефект в `crossref`: он гасил отказ
через `base._get_json`, который возвращает `None`, а не поднимает исключение.
Правка не трогает общий помощник (им пользуются другие адаптеры) — `crossref`
получил собственный метод запроса, по образцу `semanticscholar._request`.

Здесь проверяется обратное: транспортный отказ поднимается как `SearchError`,
а пустая выдача при УСПЕШНОМ ответе (`noRecordsMatch`, ноль совпадений, пустой
`message.items`) как была результатом, так и осталась — без исключения. Заодно
закрыты тестами `Candidate.pdf_url` (Semantic Scholar, arXiv) и передача
формулировки в тело запроса КиберЛенинки: с Т-16 (РЕШ-68) она ищет через
`POST /api/search`, а не через OAI-PMH. Сети нет — все ответы подменены respx.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search.arxiv import API_URL as ARXIV_API_URL
from app.search.arxiv import ArxivAdapter
from app.search.base import SearchError
from app.search.crossref import API_URL as CROSSREF_API_URL
from app.search.crossref import CrossrefAdapter
from app.search.cyberleninka import API_URL as CYBERLENINKA_API_URL
from app.search.cyberleninka import CyberLeninkaAdapter
from app.search.semanticscholar import API_URL as S2_API_URL
from app.search.semanticscholar import SemanticScholarAdapter


def _query(
    text: str = "catalyst deactivation", lang: str = "en", source_type: SourceType = SourceType.PUBLICATION
) -> SearchQuery:
    return SearchQuery(text=text, source_type=source_type, lang=lang, rationale="тест")


# ── arXiv ────────────────────────────────────────────────────────────────

_ARXIV_EMPTY_FEED = '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

_ARXIV_ONE_ENTRY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    "<entry>"
    "<id>http://arxiv.org/abs/2401.01234v2</id>"
    "<title>Catalyst deactivation study</title>"
    "<summary>Обзор механизмов дезактивации.</summary>"
    "<published>2024-03-01T00:00:00Z</published>"
    "<author><name>Ivan Petrov</name></author>"
    "</entry>"
    "</feed>"
)


@respx.mock
async def test_arxiv_500_поднимает_searcherror():
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(500, text="internal error"))
    async with httpx.AsyncClient() as client:
        adapter = ArxivAdapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)


@respx.mock
async def test_arxiv_нечитаемое_тело_поднимает_searcherror():
    """200, но тело не XML (прокси, WAF, обрезанный ответ) — тот же
    транспортный отказ, что и HTTP-ошибка, а не тихое «ничего не нашли»."""
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, text="<not-xml this is broken"))
    async with httpx.AsyncClient() as client:
        adapter = ArxivAdapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)


@respx.mock
async def test_arxiv_успешный_ответ_без_совпадений_даёт_пустой_список():
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, text=_ARXIV_EMPTY_FEED))
    async with httpx.AsyncClient() as client:
        adapter = ArxivAdapter(client)
        found = await adapter.search(_query(), limit=5)
    assert found == []


@respx.mock
async def test_arxiv_pdf_url_выводится_из_id_без_доп_запросов():
    route = respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, text=_ARXIV_ONE_ENTRY))
    async with httpx.AsyncClient() as client:
        found = await ArxivAdapter(client).search(_query(), limit=5)

    assert route.call_count == 1  # ни одного дополнительного запроса
    assert len(found) == 1
    assert str(found[0].pdf_url) == "https://arxiv.org/pdf/2401.01234v2"


async def test_arxiv_русская_формулировка_пропускается_без_запроса():
    """Не отказ и не сеть: индекс англоязычный, planner обязан прислать en."""
    async with httpx.AsyncClient() as client:
        found = await ArxivAdapter(client).search(_query(text="катализатор", lang="ru"), limit=5)
    assert found == []


# ── Semantic Scholar ────────────────────────────────────────────────────


def _s2_item(*, with_pdf: bool) -> dict:
    item: dict = {
        "title": "Catalyst deactivation review",
        "abstract": "Обзор механизмов дезактивации катализаторов.",
        "year": 2024,
        "authors": [{"name": "Ivan Petrov"}],
        "externalIds": {"DOI": "10.1/example"},
        "url": "https://www.semanticscholar.org/paper/example",
    }
    if with_pdf:
        item["openAccessPdf"] = {"url": "https://example.org/paper.pdf"}
    return item


@respx.mock
async def test_semanticscholar_500_поднимает_searcherror():
    respx.get(S2_API_URL).mock(return_value=httpx.Response(500, text="internal error"))
    async with httpx.AsyncClient() as client:
        adapter = SemanticScholarAdapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)


@respx.mock
async def test_semanticscholar_429_поднимает_searcherror_а_не_пустой_список():
    respx.get(S2_API_URL).mock(return_value=httpx.Response(429, text="rate limited"))
    async with httpx.AsyncClient() as client:
        adapter = SemanticScholarAdapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)


@respx.mock
async def test_semanticscholar_успешный_ответ_без_совпадений_даёт_пустой_список():
    respx.get(S2_API_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    async with httpx.AsyncClient() as client:
        adapter = SemanticScholarAdapter(client)
        found = await adapter.search(_query(), limit=5)
    assert found == []


@respx.mock
async def test_semanticscholar_pdf_url_из_openaccesspdf():
    respx.get(S2_API_URL).mock(return_value=httpx.Response(200, json={"data": [_s2_item(with_pdf=True)]}))
    async with httpx.AsyncClient() as client:
        found = await SemanticScholarAdapter(client).search(_query(), limit=5)

    assert len(found) == 1
    assert str(found[0].pdf_url) == "https://example.org/paper.pdf"


@respx.mock
async def test_semanticscholar_без_openaccesspdf_pdf_url_пуст():
    respx.get(S2_API_URL).mock(return_value=httpx.Response(200, json={"data": [_s2_item(with_pdf=False)]}))
    async with httpx.AsyncClient() as client:
        found = await SemanticScholarAdapter(client).search(_query(), limit=5)

    assert len(found) == 1
    assert found[0].pdf_url is None


# ── КиберЛенинка ────────────────────────────────────────────────────────
#
# Т-16 (РЕШ-68): источник ищет через POST /api/search, а не через OAI-PMH.
# Проверки отказа остались те же по смыслу — сменился транспорт: XML на GET
# стал JSON на POST, `noRecordsMatch` — ответом `found: 0` без `articles`.

_ARTICLE = {
    "name": "Катализатор гидроочистки: обзор",
    "annotation": "Обзор катализаторов гидроочистки.",
    "link": "/article/n/example",
    "authors": ["Иванов И.И."],
    "year": 2020,
    "journal": "Журнал химии",
}


@respx.mock
async def test_cyberleninka_500_поднимает_searcherror(tmp_path):
    respx.post(CYBERLENINKA_API_URL).mock(return_value=httpx.Response(500, text="internal error"))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, cache_dir=tmp_path)
        with pytest.raises(SearchError):
            await adapter.search(_query(text="катализатор гидроочистки", lang="ru"), limit=5)


@respx.mock
async def test_cyberleninka_ноль_находок_даёт_пустой_список_без_исключения(tmp_path):
    respx.post(CYBERLENINKA_API_URL).mock(return_value=httpx.Response(200, json={"found": 0}))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, cache_dir=tmp_path)
        found = await adapter.search(_query(text="катализатор гидроочистки", lang="ru"), limit=5)
    assert found == []


@respx.mock
async def test_cyberleninka_формулировка_уходит_в_тело_запроса(tmp_path):
    route = respx.post(CYBERLENINKA_API_URL).mock(
        return_value=httpx.Response(200, json={"found": 1, "articles": [_ARTICLE]})
    )
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, cache_dir=tmp_path)
        found = await adapter.search(_query(text="катализатор гидроочистки", lang="ru"), limit=5)

    assert json.loads(route.calls[0].request.content.decode("utf-8"))["q"] == "катализатор гидроочистки"
    assert len(found) == 1


@respx.mock
async def test_cyberleninka_нечитаемое_тело_поднимает_searcherror(tmp_path):
    """200, но тело не JSON — тот же транспортный отказ, что и HTTP-ошибка."""
    respx.post(CYBERLENINKA_API_URL).mock(return_value=httpx.Response(200, text="<html>не JSON</html>"))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, cache_dir=tmp_path)
        with pytest.raises(SearchError):
            await adapter.search(_query(text="катализатор гидроочистки", lang="ru"), limit=5)


# ── Crossref (РЕШ-45) ───────────────────────────────────────────────────


@respx.mock
async def test_crossref_500_поднимает_searcherror():
    respx.get(CROSSREF_API_URL).mock(return_value=httpx.Response(500, text="internal error"))
    async with httpx.AsyncClient() as client:
        adapter = CrossrefAdapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(text="катализатор гидроочистки"), limit=5)


@respx.mock
async def test_crossref_нечитаемое_тело_поднимает_searcherror():
    """200, но тело не JSON — тот же транспортный отказ, что и HTTP-ошибка,
    а не тихое «ничего не нашли»."""
    respx.get(CROSSREF_API_URL).mock(return_value=httpx.Response(200, text="<not-json this is broken"))
    async with httpx.AsyncClient() as client:
        adapter = CrossrefAdapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(text="катализатор гидроочистки"), limit=5)


@respx.mock
async def test_crossref_успешный_ответ_без_совпадений_даёт_пустой_список():
    respx.get(CROSSREF_API_URL).mock(return_value=httpx.Response(200, json={"message": {"items": []}}))
    async with httpx.AsyncClient() as client:
        adapter = CrossrefAdapter(client)
        found = await adapter.search(_query(text="катализатор гидроочистки"), limit=5)
    assert found == []
