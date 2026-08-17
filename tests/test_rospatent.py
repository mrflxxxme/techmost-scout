"""Адаптер Роспатента: разбор выдачи платформы, отказы, ключ, темп.

Сети здесь нет (инвариант №7): ответы поисковой платформы подменяются через
respx. Фикстура слеплена по форме живого ответа, снятого оркестратором с
запроса «катализатор гидроочистки дизельного топлива»: те же ключи, та же
подсветка <em> в snippet, та же дата вида «2020.06.25».
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search import build_adapters
from app.search.base import SearchError
from app.search.rospatent import SEARCH_PLATFORM_URL, RospatentAdapter

APPLICANT = 'АКЦИОНЕРНОЕ ОБЩЕСТВО "ГАЗПРОМНЕФТЬ-ОМСКИЙ НПЗ"'


def _hit(**overrides) -> dict:
    """Запись выдачи по форме живого ответа платформы."""
    hit = {
        "id": "WO2020130881A1_20200625",
        "index": "patsearch",
        "common": {
            "publishing_office": "WO",
            "document_number": "2020130881",
            "kind": "A1",
            "publication_date": "2020.06.25",
            "application": {"number": "2018145678", "filing_date": "2018.12.20"},
            "classification": {"ipc": [{"fullname": "B01J23/882"}, {"fullname": "B01J21/04"}]},
        },
        "biblio": {
            "ru": {
                "title": "КАТАЛИЗАТОР ГИДРООЧИСТКИ ДИЗЕЛЬНОГО ТОПЛИВА",
                "applicant": [{"name": APPLICANT}],
                "inventor": [{"name": "КЛИМОВ, Олег Владимирович"}, {"name": "НАДЕИНА, Ксения Александровна"}],
            },
            "en": {
                "title": "DIESEL FUEL HYDROTREATING CATALYST",
                "applicant": [{"name": "GAZPROMNEFT-OMSK REFINERY"}],
                "inventor": [{"name": "KLIMOV, Oleg Vladimirovich"}],
            },
        },
        "snippet": {
            "title": "<em>КАТАЛИЗАТОР</em> ГИДРООЧИСТКИ ДИЗЕЛЬНОГО ТОПЛИВА",
            "description": "Изобретение относится к <em>катализаторам</em> гидроочистки &amp; их приготовлению.",
            "lang": "ru",
            "applicant": APPLICANT,
        },
    }
    hit.update(overrides)
    return hit


def _payload(*hits: dict, total: int = 5772) -> dict:
    return {"available": total, "total": total, "timings": {"search": 42}, "hits": list(hits)}


def _query(text: str = "катализатор гидроочистки дизельного топлива") -> SearchQuery:
    return SearchQuery(text=text, source_type=SourceType.PATENT, lang="ru", rationale="тест")


@pytest.fixture
def adapter_factory():
    def make(client: httpx.AsyncClient, **kwargs) -> RospatentAdapter:
        # Темп проверяется отдельным тестом; остальным ждать секунду незачем
        interval = kwargs.pop("min_interval_seconds", 0.0)
        params: dict = {"api_key": "test-key", "contact_email": "owner@example.org"}
        params.update(kwargs)
        adapter = RospatentAdapter(client, **params)
        adapter.min_interval_seconds = interval
        return adapter

    return make


# ── Разбор выдачи ───────────────────────────────────────────────────────────
@respx.mock
async def test_разбирает_выдачу_в_кандидатов(adapter_factory):
    respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(200, json=_payload(_hit())))
    async with httpx.AsyncClient() as client:
        found = await adapter_factory(client).search(_query(), limit=3)

    assert len(found) == 1
    card = found[0]
    assert card.source_type is SourceType.PATENT, "выдача Роспатента — патенты, это меняет трактовку в карточке"
    assert card.provider == "rospatent"
    assert card.title == "КАТАЛИЗАТОР ГИДРООЧИСТКИ ДИЗЕЛЬНОГО ТОПЛИВА"
    # Заявитель — прямое поле правообладателя, ради него источник и нужен
    assert card.org == APPLICANT
    assert card.year == 2020
    # Номер публикации целиком: без страны номер не уникален
    assert card.raw_id == "WO2020130881A1"
    assert str(card.url) == "https://patents.google.com/patent/WO2020130881A1"
    assert card.authors[0] == "КЛИМОВ, Олег Владимирович"


@respx.mock
async def test_разметка_подсветки_снята_из_доказательства(adapter_factory):
    """Текст evidence читает человек: тегов <em> и сущностей в нём быть не может."""
    respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(200, json=_payload(_hit())))
    async with httpx.AsyncClient() as client:
        found = await adapter_factory(client).search(_query(), limit=3)

    assert "<em>" not in found[0].snippet and "</em>" not in found[0].snippet
    assert found[0].snippet == "Изобретение относится к катализаторам гидроочистки & их приготовлению."


@respx.mock
async def test_английское_название_как_запасной_вариант(adapter_factory):
    hit = _hit()
    hit["biblio"]["ru"] = {"applicant": [{"name": "ООО «Пример»"}]}
    respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(200, json=_payload(hit)))
    async with httpx.AsyncClient() as client:
        found = await adapter_factory(client).search(_query(), limit=3)

    assert found[0].title == "DIESEL FUEL HYDROTREATING CATALYST"
    assert found[0].org == "ООО «Пример»"


@respx.mock
async def test_запись_без_названия_или_номера_отбрасывается(adapter_factory):
    without_number = _hit(id="", common={"publication_date": "2021.01.01"})
    without_title = _hit()
    without_title["biblio"] = {}
    without_title["snippet"] = {}
    respx.post(SEARCH_PLATFORM_URL).mock(
        return_value=httpx.Response(200, json=_payload(without_number, without_title, _hit()))
    )
    async with httpx.AsyncClient() as client:
        found = await adapter_factory(client).search(_query(), limit=10)

    assert [c.raw_id for c in found] == ["WO2020130881A1"]


@respx.mock
async def test_успешный_ответ_без_записей_это_пустой_список(adapter_factory):
    """Пустая выдача при живом ответе — не отказ источника (инвариант №5)."""
    respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(200, json=_payload(total=0)))
    async with httpx.AsyncClient() as client:
        assert await adapter_factory(client).search(_query(), limit=5) == []


# ── Запрос ──────────────────────────────────────────────────────────────────
@respx.mock
async def test_запрос_уходит_с_bearer_и_честным_представлением(adapter_factory):
    route = respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(200, json=_payload()))
    async with httpx.AsyncClient() as client:
        await adapter_factory(client).search(_query("прочность бетона"), limit=7)

    request = route.calls[0].request
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["Content-Type"] == "application/json"
    # Контакт владельца берётся из настроек, а не зашит в адаптер
    assert request.headers["User-Agent"] == "TechMostScout/1.0 (mailto:owner@example.org)"
    assert json.loads(request.content) == {"qn": "прочность бетона", "limit": 7}


@respx.mock
async def test_темп_не_чаще_одного_запроса_в_секунду(adapter_factory):
    """Пауза объявлена — проверяем фактом, а не значением поля: троттлинг при
    нулевом интервале базовый `_throttle` пропускает молча (П-4 аудита Т-3)."""
    respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(200, json=_payload()))
    async with httpx.AsyncClient() as client:
        adapter = adapter_factory(client)
        assert RospatentAdapter.min_interval_seconds >= 1.0, "лимит платформы неизвестен — темп должен быть щадящим"
        adapter.min_interval_seconds = RospatentAdapter.min_interval_seconds
        started = time.perf_counter()
        await adapter.search(_query(), limit=1)
        await adapter.search(_query("второй запрос"), limit=1)
        elapsed = time.perf_counter() - started

    # Первому запросу ждать не от чего, значит интервал ровно один.
    # 0,9 — допуск на разрешение системного таймера.
    assert elapsed >= adapter.min_interval_seconds * 0.9, f"запросы ушли подряд, за {elapsed:.3f} с"


# ── Отказы ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [401, 403, 429, 500])
@respx.mock
async def test_отказ_платформы_поднимает_search_error(adapter_factory, status):
    """Инвариант №5 в редакции РЕШ-43: отказ доступа и лимит не превращаются в
    пустой успех — иначе мёртвый ключ читается как «патентов по задаче нет»."""
    respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(status, json={"error": "нет"}))
    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError) as failure:
            await adapter_factory(client).search(_query(), limit=5)

    assert "test-key" not in str(failure.value), "значение ключа в сообщении об ошибке недопустимо (инвариант №3)"


@respx.mock
async def test_транспортный_отказ_поднимает_search_error(adapter_factory):
    respx.post(SEARCH_PLATFORM_URL).mock(side_effect=httpx.ConnectError("соединение не установлено"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await adapter_factory(client).search(_query(), limit=5)


@respx.mock
async def test_нечитаемое_тело_ответа_поднимает_search_error(adapter_factory):
    respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(200, text="не json"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await adapter_factory(client).search(_query(), limit=5)


# ── Ключ ────────────────────────────────────────────────────────────────────
async def test_без_ключа_поиск_пуст_и_здоровье_отрицательно():
    async with httpx.AsyncClient() as client:
        adapter = RospatentAdapter(client)
        assert await adapter.search(_query(), limit=5) == []
        ok, detail = await adapter.health()

    assert ok is False
    assert "ROSPATENT_API_KEY" in detail


async def test_источник_включается_только_с_ключом():
    async with httpx.AsyncClient() as client:
        assert "rospatent" not in {a.name for a in build_adapters(client, None)}
        with_key = build_adapters(client, {"rospatent_api_key": "k", "crossref_mailto": "owner@example.org"})

    adapter = next(a for a in with_key if a.name == "rospatent")
    assert isinstance(adapter, RospatentAdapter)
    # Почта владельца доезжает из настроек до заголовка запроса
    assert adapter.user_agent == "TechMostScout/1.0 (mailto:owner@example.org)"


@respx.mock
async def test_доктор_проверяет_ключ_живой_пробой(adapter_factory):
    respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(200, json=_payload(_hit())))
    async with httpx.AsyncClient() as client:
        ok, detail = await adapter_factory(client).health()

    assert ok is True
    assert "1" in detail

    respx.post(SEARCH_PLATFORM_URL).mock(return_value=httpx.Response(401, json={"error": "нет"}))
    async with httpx.AsyncClient() as client:
        ok, detail = await adapter_factory(client).health()

    assert ok is False
    assert "401" in detail
