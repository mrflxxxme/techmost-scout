"""Отказ трёх источников доходит до вызывающего кода, а не гасится в [] (Ф-5).

`arxiv`, `semanticscholar`, `cyberleninka` до этой правки ловили транспортный
отказ (HTTP-ошибка, 429, нечитаемый ответ) внутри адаптера и превращали его в
обычный пустой список — неотличимый от честного «источник ответил, но ничего
не нашёл». Из-за этого ветка `unavailable` в оркестраторе была недостижима:
`_safe_search` мог поймать только исключение, а адаптер его не поднимал.

Здесь проверяется обратное: транспортный отказ поднимается как `SearchError`,
а пустая выдача при УСПЕШНОМ ответе (`noRecordsMatch`, ноль совпадений) как
была результатом, так и осталась — без исключения. Заодно закрыты тестами
`Candidate.pdf_url` (Semantic Scholar, arXiv) и передача `set` в запрос
КиберЛенинки без предупреждения. Сети нет — все ответы подменены respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search.arxiv import API_URL as ARXIV_API_URL
from app.search.arxiv import ArxivAdapter
from app.search.base import SearchError
from app.search.cyberleninka import BASE_URL as CYBERLENINKA_BASE_URL
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

_OAI_NO_RECORDS_MATCH = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
    "<responseDate>2026-08-12T00:00:00Z</responseDate>"
    '<request verb="ListRecords">https://cyberleninka.ru/oai</request>'
    '<error code="noRecordsMatch">Записей не найдено</error>'
    "</OAI-PMH>"
)


def _oai_record(*, title: str = "Катализатор гидроочистки: обзор") -> str:
    return (
        "<record>"
        "<header><identifier>oai:cyberleninka.ru:article/n/example</identifier>"
        "<datestamp>2020-01-01</datestamp></header>"
        "<metadata><oai_dc:dc "
        'xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title>"
        "<dc:creator>Иванов И.И.</dc:creator>"
        "<dc:publisher>Журнал химии</dc:publisher>"
        "<dc:identifier>https://cyberleninka.ru/article/n/example</dc:identifier>"
        "</oai_dc:dc></metadata>"
        "</record>"
    )


def _oai_list_records(*records: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        "<responseDate>2026-08-12T00:00:00Z</responseDate>"
        '<request verb="ListRecords">https://cyberleninka.ru/oai</request>'
        f"<ListRecords>{''.join(records)}</ListRecords>"
        "</OAI-PMH>"
    )


@respx.mock
async def test_cyberleninka_500_поднимает_searcherror(tmp_path):
    respx.get(CYBERLENINKA_BASE_URL).mock(return_value=httpx.Response(500, text="internal error"))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, oai_set="cas", cache_dir=tmp_path)
        with pytest.raises(SearchError):
            await adapter.search(_query(text="катализатор гидроочистки", lang="ru"), limit=5)


@respx.mock
async def test_cyberleninka_norecordsmatch_даёт_пустой_список_без_исключения(tmp_path):
    respx.get(CYBERLENINKA_BASE_URL).mock(return_value=httpx.Response(200, text=_OAI_NO_RECORDS_MATCH))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, oai_set="cas", cache_dir=tmp_path)
        found = await adapter.search(_query(text="катализатор гидроочистки", lang="ru"), limit=5)
    assert found == []


@respx.mock
async def test_cyberleninka_набор_уходит_в_запрос_и_предупреждения_нет(tmp_path, caplog):
    route = respx.get(CYBERLENINKA_BASE_URL).mock(
        return_value=httpx.Response(200, text=_oai_list_records(_oai_record()))
    )
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, oai_set="cas", cache_dir=tmp_path)
        with caplog.at_level("WARNING", logger="app.search.cyberleninka"):
            found = await adapter.search(_query(text="катализатор гидроочистки", lang="ru"), limit=5)

    assert route.calls[0].request.url.params["set"] == "cas"
    assert len(found) == 1
    assert not any("набор (set) не задан" in record.message for record in caplog.records)
