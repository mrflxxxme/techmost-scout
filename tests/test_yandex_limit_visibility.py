"""Исчерпание платного предела Яндекса видно снаружи (Ц-1 пост-аудита Э2-Б).

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ
    После волны Э2-Б запас предела кончился: два прохода по две web-формулировки
    и по одной конкурсной дают ровно шесть платных запросов — весь
    `max_queries = 6`. Владелец решил предел не поднимать, а сделать исчерпание
    видимым: пропущенная формулировка обязана попасть в проблемы прогона,
    иначе в отчёте она неотличима от «источник ответил и ничего не нашёл».

    До правки исчерпание жило только в `log.warning`, а лога прогона снаружи не
    видно — тот же класс, что инвариант №5 («скрытый отказ хуже видимого»).

Сети нет: и запуск поиска, и опрос операции подменяются через respx.
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter


def _xml(*docs: str) -> str:
    body = "".join(docs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<yandexsearch version='1.0'><response><results><grouping><group>{body}"
        "</group></grouping></results></response></yandexsearch>"
    )


def _doc(url: str) -> str:
    return (
        "<doc>"
        f"<url>{url}</url><domain>example.org</domain>"
        "<title>Заголовок про катализатор</title><modtime>20240115T103000</modtime>"
        "<passages><passage>Фрагмент страницы.</passage></passages>"
        "</doc>"
    )


def _operation_done(xml: str) -> dict:
    return {"done": True, "response": {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}}


def _mock_ok(xml: str) -> None:
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(200, json=_operation_done(xml)))


def _query(text: str = "катализатор гидроочистки") -> SearchQuery:
    return SearchQuery(text=text, source_type=SourceType.WEB, lang="ru", rationale="тест")


@pytest.fixture
def make_adapter():
    def build(client: httpx.AsyncClient, **kwargs) -> YandexSearchAdapter:
        params = {
            "api_key": "test-key",
            "folder_id": "b1gtest",
            "poll_interval_seconds": 0.0,
            "max_wait_seconds": 5.0,
        }
        params.update(kwargs)
        return YandexSearchAdapter(client, **params)

    return build


@respx.mock
async def test_пропуск_по_пределу_попадает_в_проблемы_прогона(make_adapter):
    """Формулировка, не отправленная из-за предела, называется по тексту в проблемах.

    Проверяется ровно то, чего не хватало отчёту: не «источник вернул ноль», а
    «источник эту формулировку не спрашивал, потому что кончился платный
    предел». Без записи расход платного источника снова стал бы невидим.
    """
    _mock_ok(_xml(_doc("https://a.example.org/page")))
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client, max_queries=2)
        await adapter.search(_query("первая формулировка"), limit=5)
        await adapter.search(_query("вторая формулировка"), limit=5)
        skipped = await adapter.search(_query("третья формулировка"), limit=5)

    assert skipped == [], "формулировка сверх предела не должна уходить в платный источник"
    assert adapter.queries_used == 2
    problems = adapter.limit_problems
    assert len(problems) == 1, "пропуск по пределу снова молчит — в проблемах прогона его нет"
    assert "третья формулировка" in problems[0], "по записи не понять, какую формулировку не спросили"
    assert "предел" in problems[0] and "2" in problems[0], "в записи нет причины пропуска и величины предела"
    assert "yandex_search" in problems[0], "по записи не понять, у какого источника кончился предел"


@respx.mock
async def test_пустая_выдача_источника_проблемой_не_считается(make_adapter):
    """Отличимость от пустого ответа: источник спросили, он ответил, находок нет.

    Пустой результат при успешном ответе отказом не является (инвариант №5 в
    редакции РЕШ-43), и в проблемы прогона он попадать не должен — иначе
    запись о пределе потеряет смысл, слившись с обычной пустой выдачей.
    """
    _mock_ok(_xml())
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client, max_queries=6)
        found = await adapter.search(_query("формулировка без находок"), limit=5)

    assert found == []
    assert adapter.queries_used == 1, "запрос был отправлен и оплачен — это не пропуск"
    assert adapter.limit_problems == [], "пустая выдача записана как проблема предела — записи стали неотличимы"


@respx.mock
async def test_каждая_пропущенная_формулировка_считается_отдельно(make_adapter):
    """Проблем столько же, сколько пропущенных формулировок — это мера недобора.

    Одна запись «предел исчерпан» на весь прогон не сказала бы, сколько именно
    вопросов источнику не задали, а это и есть цена решения не поднимать предел.
    """
    _mock_ok(_xml(_doc("https://a.example.org/page")))
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client, max_queries=1)
        await adapter.search(_query("оплаченная формулировка"), limit=5)
        for number in range(3):
            await adapter.search(_query(f"пропущенная формулировка {number}"), limit=5)

    assert adapter.queries_used == 1
    assert len(adapter.limit_problems) == 3
    assert all("пропущенная формулировка" in problem for problem in adapter.limit_problems)


@respx.mock
async def test_проблемы_забираются_один_раз_и_не_повторяются_во_втором_проходе(make_adapter):
    """`drain_limit_problems` отдаёт накопленное и обнуляет счёт.

    Проблемы собираются на каждый проход поиска отдельно (`SearchOutcome.problems`
    живёт внутри `gather_candidates`), поэтому записи первого прохода не должны
    приезжать во второй ещё раз: удвоенная запись читалась бы как удвоенный
    недобор.
    """
    _mock_ok(_xml(_doc("https://a.example.org/page")))
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client, max_queries=0)
        await adapter.search(_query("формулировка первого прохода"), limit=5)
        first = adapter.drain_limit_problems()
        second = adapter.drain_limit_problems()

        await adapter.search(_query("формулировка второго прохода"), limit=5)
        third = adapter.drain_limit_problems()

    assert len(first) == 1 and "формулировка первого прохода" in first[0]
    assert second == [], "забранные проблемы вернулись повторно — недобор задваивается"
    assert len(third) == 1 and "формулировка второго прохода" in third[0]


@respx.mock
async def test_запись_о_пределе_не_делает_источник_недоступным(make_adapter):
    """Исчерпан наш собственный бюджет, а не источник: `SearchError` не поднимается.

    Иначе Яндекс уехал бы в `sources_unavailable` — «не ответил», хотя он
    исправно отвечал на все оплаченные формулировки. Отчёт получил бы неверную
    причину вместо честной «предел трат исчерпан».
    """
    _mock_ok(_xml(_doc("https://a.example.org/page")))
    async with httpx.AsyncClient() as client:
        adapter = make_adapter(client, max_queries=0)
        found, raw = await adapter.search_raw(_query("формулировка сверх предела"), limit=5)

    assert found == [] and raw == []
    assert adapter.limit_problems, "пропуск по пределу не записан"
