"""Адаптер Яндекса: два контура, предел трат, разбор XML, отказы.

Сети нет: и запуск поиска, и опрос операции подменяются через respx. XML взят
по образцу фактического ответа Яндекса, снятого живым вызовом 11 августа 2026, —
включая подсветку слов в <hlword> и время правки страницы в modtime.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import httpx
import pytest
import respx

from app.llm import LLMRouter
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import Candidate, SearchPlan, SearchQuery, SourceType
from app.search import build_adapters
from app.search.base import SearchAdapter, SearchError, normalize_url
from app.search.yandex_search import (
    CONTEST_OUTCOME_MARKERS,
    CONTEST_SITES,
    GRANT_SITES,
    OPERATION_URL,
    SEARCH_ASYNC_URL,
    YandexSearchAdapter,
    has_contest_outcome_marker,
)


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
    # З-5 (находка В-2 аудита Э2-Г): раньше здесь стояло card.org ==
    # "pure.manchester.ac.uk" и card.year == 2024 — это закрепляло дефект,
    # из-за которого домен и время правки страницы уезжали в отчёт как
    # подтверждённые «организация» и «год публикации» (инвариант №1). Разбор
    # адаптера больше не строит догадки: обе догадки решает стадия extractor,
    # а на её отказе честнее пустое поле, чем выдумка.
    assert card.org is None
    assert card.year is None
    assert card.snippet == "Первый фрагмент. Второй фрагмент."
    # Ключ склейки общий с другими веб-источниками
    assert card.raw_id == "pure.manchester.ac.uk/lab"


@respx.mock
async def test_search_raw_несёт_domain_и_modtime_для_стадии_extractor(make_adapter):
    """Критерий 3 задачи З-5: пустые `org`/`year` у разбора адаптера не значат
    потерю данных — сырые записи по-прежнему несут `domain` и `modtime`, из
    которых стадия extractor вправе сделать вывод (`:442-446` — комментарий
    у `_raw_records`, ради которого правка и делалась)."""
    _mock_ok(_xml(_doc("https://pure.manchester.ac.uk/lab/", "Лаборатория", domain="pure.manchester.ac.uk")))
    async with httpx.AsyncClient() as client:
        candidates, raw = await make_adapter(client).search_raw(_query(), limit=10)

    assert len(candidates) == 1
    assert candidates[0].org is None and candidates[0].year is None

    assert len(raw) == 1
    assert raw[0]["domain"] == "pure.manchester.ac.uk"
    assert raw[0]["modtime"] == "20240115T103000"
    assert raw[0]["url"] == "https://pure.manchester.ac.uk/lab/"


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
async def test_отказ_внутри_xml_поднимает_searcherror(make_adapter):
    """Яндекс сообщает об отказе (исчерпанный лимит, нет прав) внутри XML, а не
    кодом HTTP — это транспортный отказ (инвариант №5, РЕШ-45), не тихий
    пустой список: иначе «0» неотличимо от честного «источник ничего не
    нашёл», и порог квоты (шесть платных запросов) нечем измерить."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><yandexsearch><response>'
        '<error code="55">Превышен лимит запросов</error>'
        "</response></yandexsearch>"
    )
    _mock_ok(xml)
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=10)


@respx.mock
async def test_незавершившаяся_операция_поднимает_searcherror(make_adapter):
    """Операция не завершилась за отведённое время — источник не отдал
    результат, это отказ (инвариант №5, РЕШ-45), а не пустая выдача."""
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(200, json={"done": False}))
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client, max_wait_seconds=0.2)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)


@respx.mock
async def test_поиск_не_запустился_поднимает_searcherror(make_adapter):
    """HTTP-отказ запуска платного поиска — транспортный отказ (РЕШ-45)."""
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(500, text="internal error"))
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)


@respx.mock
async def test_опрос_операции_не_отвечает_поднимает_searcherror(make_adapter):
    """HTTP-отказ при опросе уже запущенной операции — тот же транспортный
    отказ, что и при запуске (РЕШ-45)."""
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(503, text="unavailable"))
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client)
        with pytest.raises(SearchError):
            await adapter.search(_query(), limit=5)


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
async def test_страница_из_другого_веб_источника_склеивается_в_одного_кандидата(make_adapter):
    """Главная причина общего ключа склейки: один адрес — один кандидат.

    Раньше вторым источником в паре был Google (адаптер удалён, РЕШ-174).
    Ключ склейки — общая функция `normalize_url`, а не что-то специфичное
    для Яндекса, поэтому её достаточно проверить напрямую вторым кандидатом,
    без второго живого адаптера.
    """
    _mock_ok(_xml(_doc("https://pure.manchester.ac.uk/lab/", "Лаборатория")))
    async with httpx.AsyncClient() as client:
        from_yandex = await make_adapter(client).search(_query(), limit=5)

    other_source = Candidate(
        source_type=SourceType.WEB,
        title="Lab page",
        url="https://www.pure.manchester.ac.uk/lab",
        snippet="…",
        raw_id=normalize_url("https://www.pure.manchester.ac.uk/lab"),
        provider="другой_веб_источник",
    )

    assert from_yandex[0].raw_id == other_source.raw_id
    assert from_yandex[0].dedup_key() == other_source.dedup_key()


# ── Т-23: конкурсные формулировки через контур Яндекса ─────────────────────
#
# Своего адаптера под конкурсы нет и не будет (РЕШ-79 п. 3, РЕШ-88): площадки
# читаются сужением `site:` в том же платном контуре. Ни один тест ниже не
# регистрирует маршрута на `devpost.com` — обращение к `devpost.com/api/*`
# запрещено (РЕШ-87), и в сеть здесь уходит только поисковый URL Яндекса.


def _contest_query(text: str = "хакатон решение команды катализатор") -> SearchQuery:
    return _query(text=text, lang="ru", source_type=SourceType.CONTEST)


@respx.mock
async def test_конкурсная_формулировка_с_якорем_итогов_уходит_без_сужения(make_adapter):
    """Критерий 1 (РЕШ-107, РЕШ-117): формулировка с якорем итогов ищет
    опубликованный разбор победы, а не саму площадку, и обязана уйти в
    Яндекс БЕЗ `site:` - иначе выдача снова свелась бы к рекламным страницам
    площадок, из-за которых конкурсная очередь дала 0 карточек при 20
    кандидатах на прогон (РЕШ-107)."""
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(_doc("https://university.example.org/news/1"))))
    )
    async with httpx.AsyncClient() as client:
        await make_adapter(client).search(_contest_query("итоги хакатона победитель диагностика УЗИ"), limit=5)

    sent = json.loads(route.calls[0].request.content)["query"]["queryText"]
    assert "site:" not in sent, "формулировка с якорем итогов не должна сужаться по площадкам"
    assert sent == "итоги хакатона победитель диагностика УЗИ", "текст формулировки не должен меняться"


def test_contest_outcome_markers_состав_дословно_по_реш_117():
    """Мутация (а): якорь, убранный из CONTEST_OUTCOME_MARKERS, обязан ронять
    именно этот тест. Состав сверяется с жёстко заданным списком, а не через
    параметризацию по самому кортежу: `@pytest.mark.parametrize("marker",
    CONTEST_OUTCOME_MARKERS)` собирает список параметров уже ИЗ изменённого
    кортежа на этапе сбора тестов, поэтому удалённый якорь не даёт ни одного
    провалившегося случая — такая проверка ничего не поймает."""
    assert set(CONTEST_OUTCOME_MARKERS) == {
        "победител",
        "призёр",
        "призер",
        "итоги хакатона",
        "проект команды",
    }


@pytest.mark.parametrize("marker", ["победител", "призёр", "призер", "итоги хакатона", "проект команды"])
def test_has_contest_outcome_marker_ловит_каждый_якорь_по_отдельности(marker):
    """Мутация (а) постановки задачи: якорь, убранный из CONTEST_OUTCOME_MARKERS,
    обязан ронять именно этот тест - по одному случаю на каждый якорь. Список
    якорей задан дословно, а не параметризацией по самому кортежу (см. докстроку
    `test_contest_outcome_markers_состав_дословно_по_реш_117`)."""
    assert has_contest_outcome_marker(f"текст формулировки с якорем {marker} внутри") is True


def test_has_contest_outcome_marker_false_без_единого_якоря():
    assert has_contest_outcome_marker("хакатон решение команды диагностика УЗИ") is False


@respx.mock
async def test_конкурсная_формулировка_доходит_до_адаптера_и_сужается_до_площадок(
    make_adapter, settings, fake_provider_factory
):
    """Критерий 1: тип `contest` доходит до платного контура, а не отсеивается.

    Проверяется именно ДОСТАВКА, а не сборка строки: план идёт через
    `gather_candidates`, где оркестратор отдаёт адаптеру только формулировки с
    типом из `source_types` (`orchestrator.py`). Убери `CONTEST` из
    `YandexSearchAdapter.source_types` — запрос не уйдёт вовсе, и тест упадёт
    на числе вызовов, а не на содержимом строки.
    """
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json=_operation_done(_xml(_doc("https://devpost.com/software/team-solution", domain="devpost.com"))),
        )
    )
    plan = SearchPlan(queries=[_contest_query()])
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client)
        # Стадия extractor (Э2-5) к задаче не относится и требует модели:
        # проверяем разбор самого адаптера, а не нормализацию выдачи.
        adapter.needs_extraction = False
        router = LLMRouter(settings, providers={"fake": fake_provider_factory("fake")})
        adapters: list[SearchAdapter] = [adapter]
        outcome = await ScoutPipeline(settings=settings, router=router, adapters=adapters).gather_candidates(plan)

    assert route.call_count == 1, "конкурсная формулировка не дошла до адаптера — оркестратор отсеял её по типу"
    sent = json.loads(route.calls[0].request.content)["query"]["queryText"]
    assert "site:devpost.com" in sent
    assert "site:hacks-ai.ru" in sent, "нет ни одной российской площадки — сужение собрано не по CONTEST_SITES"
    assert outcome.hits_by_name == {"yandex_search": 1}
    # Тип кандидата — тип формулировки: иначе потолок зрелости (Т-22) и
    # отдельная секция отчёта (Т-24) конкурсных кандидатов не увидят.
    assert outcome.candidates[0].source_type is SourceType.CONTEST


@respx.mock
async def test_конкурсное_сужение_перечисляет_все_площадки(make_adapter):
    """Список площадок берётся целиком: пропавшая площадка — потерянная выдача."""
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(_doc("https://all-cups.ru/champ/1"))))
    )
    async with httpx.AsyncClient() as client:
        await make_adapter(client).search(_contest_query(), limit=5)

    sent = json.loads(route.calls[0].request.content)["query"]["queryText"]
    for site in CONTEST_SITES:
        assert f"site:{site}" in sent, f"площадка {site} выпала из конкурсного сужения"
    # Текст формулировки сохраняется: сужение добавляется к нему, а не заменяет
    assert sent.startswith("хакатон решение команды катализатор")


@respx.mock
async def test_грантовая_формулировка_не_получает_конкурсных_площадок(make_adapter):
    """Критерий 2: гранты остаются в реестрах РНФ и Фонда содействия.

    Слово «конкурс» есть и среди грантовых маркеров (`GRANT_INTENT_MARKERS`), и
    в конкурсных формулировках — если подмешать площадки грантовому запросу,
    он уедет из реестров поддержанных проектов на хакатоны.
    """
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(_doc("https://rscf.ru/project/1"))))
    )
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client)
        await adapter.search(
            _query(text="конкурс поддержанные проекты РНФ катализатор", lang="ru", source_type=SourceType.WEB),
            limit=5,
        )

    sent = json.loads(route.calls[0].request.content)["query"]["queryText"]
    for site in GRANT_SITES:
        assert f"site:{site}" in sent
    for site in CONTEST_SITES:
        assert f"site:{site}" not in sent, f"грантовая формулировка увела выдачу на конкурсную площадку {site}"


@respx.mock
async def test_конкурсная_формулировка_не_получает_грантовых_реестров(make_adapter):
    """Обратное направление того же критерия: у конкурсов свои площадки.

    Реестры РНФ и Фонда содействия перечисляют поддержанные ПРОЕКТЫ, а не
    решения команд, — подмешивать их к конкурсному запросу значит тратить тот
    же платный лимит на чужую выдачу.
    """
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(_doc("https://codenrock.com/champ/2"))))
    )
    async with httpx.AsyncClient() as client:
        await make_adapter(client).search(_contest_query("конкурс грант хакатон катализатор"), limit=5)

    sent = json.loads(route.calls[0].request.content)["query"]["queryText"]
    for site in GRANT_SITES:
        assert f"site:{site}" not in sent, "конкурсная формулировка получила грантовые реестры"


@respx.mock
async def test_конкурсные_формулировки_не_поднимают_предел_платных_запросов(make_adapter, caplog):
    """Критерий 4: предел прогона прежний — шесть запросов ПО УМОЛЧАНИЮ.

    `max_queries` здесь не задаётся нарочно: тест обязан падать, если предел
    подняли в самом адаптере. Рост предела умножается на второй проход и на
    потолок 110 ₽, поэтому конкурсные формулировки конкурируют за тот же лимит.
    """
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(_doc("https://sk.ru/news/1"))))
    )
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client)
        with caplog.at_level(logging.WARNING, logger="app.search.yandex_search"):
            for _ in range(6):
                await adapter.search(_contest_query(), limit=5)
            seventh = await adapter.search(_contest_query("седьмая конкурсная формулировка"), limit=5)

    assert route.call_count == 6, "предел платных запросов на прогон изменился"
    assert adapter.queries_used == 6
    assert seventh == []
    assert any("предел 6 платных запросов на прогон исчерпан" in record.getMessage() for record in caplog.records), (
        "исчерпание предела не попало в лог — расход платного источника снова невидим"
    )


def test_в_коде_нет_прямого_обращения_к_api_devpost():
    """Критерий 3: Devpost читается только через чужой поисковый индекс.

    `devpost.com/api/hackathons` отвечает 200 и JSON без ключа, но их условия
    запрещают роботить сайт (РЕШ-87): границу владелец провёл по строке
    `site:devpost.com` внутри поисковой формулировки. Машинная проверка — по
    исходникам, а не по обещанию в ревью.
    """
    backend = Path(__file__).resolve().parents[1] / "backend"
    sources = list(backend.rglob("*.py"))
    assert sources, "не найдено ни одного файла backend/ — проверка ничего не проверяет"
    for path in sources:
        # Комментарии из проверки исключены: запрет положено объяснять там, где
        # он действует, и слова «обращение к `devpost.com/api/*` запрещено»
        # рядом со списком площадок — это причина, а не нарушение.
        code = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
        )
        assert "devpost.com/api" not in code, f"{path}: прямое обращение к API Devpost запрещено (РЕШ-87)"
        # Адрес с путём или со схемой — это уже обращение к их сайту, а не
        # строка сужения: в коде Devpost имеет право быть только именем
        # площадки в списке CONTEST_SITES.
        assert "devpost.com/" not in code, f"{path}: адрес devpost.com с путём — обращение к сайту площадки"
        assert "//devpost.com" not in code, f"{path}: адрес devpost.com со схемой — обращение к сайту площадки"
