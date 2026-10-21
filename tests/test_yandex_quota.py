"""Э6-10: честный предел платных запросов Яндекса и резерв под аналоги (РЕШ-285).

Дефект на базе: проверка места (`queries_used >= max_queries`) и учёт (`+= 1`)
стояли по разные стороны сетевого вызова. Формулировки прохода стартуют
задачами одной `TaskGroup`, поэтому первые запросы всех формулировок проходили
проверку раньше, чем кто-то учитывался: 5 web + 2 analog давали 7 запросов
(ревью Э6-2в, п. 4). Здесь предел проверяется на параллельных вызовах с
задержкой ответа — только так видно, что место занимается до `await`.

Сети нет: запуск и опрос операции подменяет respx, ключ — заглушка.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
import respx

from app.llm import LLMRouter
from app.pipeline.orchestrator import ScoutPipeline
from app.pipeline.planning import normalize_plan_queries
from app.schemas import Brief, SearchPlan, SearchQuery, SourceType
from app.search.analog_sources import plan_analog_queries
from app.search.yandex_quota import (
    ANALOG_REGISTRY_SLOTS,
    ANALOG_WORLD_SLOTS,
    Verdict,
    YandexQuota,
    split_width,
)
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter

_EMPTY_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    "<yandexsearch version='1.0'><response><results><grouping><group>"
    "</group></grouping></results></response></yandexsearch>"
)

EN_A = "industrial catalyst regeneration case studies"
EN_B = "catalyst deactivation startups commercial solutions"
RU_REGISTRY = "регенерация катализатора промышленные решения"


class Flight:
    """Счётчик одновременно летящих запросов: доказывает, что замка через сеть нет."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0


def _mock_yandex() -> tuple[respx.Route, Flight]:
    """POST searchAsync отвечает с задержкой 0,05 с; опрос операции — сразу `done`."""
    flight = Flight()

    async def slow_start(request: httpx.Request) -> httpx.Response:
        flight.current += 1
        flight.peak = max(flight.peak, flight.current)
        try:
            await asyncio.sleep(0.05)
        finally:
            flight.current -= 1
        return httpx.Response(200, json={"id": "op-1"})

    route = respx.post(SEARCH_ASYNC_URL).mock(side_effect=slow_start)
    done = {"done": True, "response": {"rawData": base64.b64encode(_EMPTY_XML.encode()).decode("ascii")}}
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(200, json=done))
    return route, flight


def _sent(route: respx.Route) -> list[str]:
    return [json.loads(call.request.content)["query"]["queryText"] for call in route.calls]


def _q(text: str, source_type: SourceType = SourceType.WEB, lang: str = "ru") -> SearchQuery:
    return SearchQuery(text=text, source_type=source_type, lang=lang, rationale="тест")


def _adapter(client: httpx.AsyncClient, **kwargs) -> YandexSearchAdapter:
    adapter = YandexSearchAdapter(
        client,
        api_key="test-key",
        folder_id="b1gtest",
        poll_interval_seconds=0.0,
        max_wait_seconds=5.0,
        **kwargs,
    )
    adapter.min_interval_seconds = 0
    adapter.needs_extraction = False
    return adapter


def _pipeline(settings, adapter: YandexSearchAdapter) -> ScoutPipeline:
    return ScoutPipeline(settings=settings, router=LLMRouter(settings, providers={}), adapters=[adapter])


def _market_plan(*, web: int = 5, contest: int = 2, en: tuple[str, ...] = (EN_A, EN_B), ru: bool = True) -> SearchPlan:
    """План «Рынка» в порядке `normalize_plan_queries`: прочие, затем аналоги."""
    queries = [_q(f"веб формулировка {n}") for n in range(1, web + 1)]
    queries += [_q(f"конкурс формулировка {n}", SourceType.CONTEST) for n in range(1, contest + 1)]
    queries += [_q(text, SourceType.ANALOG, "en") for text in en]
    if ru:
        queries.append(_q(RU_REGISTRY, SourceType.ANALOG, "ru"))
    return SearchPlan(queries=queries)


def _other_sent(sent: list[str], prefix: str) -> list[str]:
    return [text for text in sent if prefix in text]


# ── Чистый учёт мест ────────────────────────────────────────────────────────


def test_раздача_в_ширину_даёт_каждой_формулировке_по_пачке_прежде_чем_второй():
    assert split_width(2, [2, 2]) == [1, 1]
    assert split_width(2, [2]) == [2]
    assert split_width(2, [3, 1]) == [1, 1]
    assert split_width(2, [1, 3]) == [1, 1]
    assert split_width(3, [3, 1]) == [2, 1]
    assert split_width(2, [2, 2, 2]) == [1, 1, 0]
    assert split_width(2, []) == []
    assert split_width(2, [0, 2]) == [0, 2]


def test_квота_резервирует_синхронно_и_не_пускает_за_предел():
    quota = YandexQuota(2)
    assert quota.reserve_other() is Verdict.GRANTED
    assert quota.reserve_other() is Verdict.GRANTED
    assert quota.reserve_other() is Verdict.LIMIT
    assert quota.reserve_analog(("x", "en")) is Verdict.LIMIT
    assert quota.used == 2


def test_квота_резерв_уходит_прочим_если_аналогов_в_проходе_нет():
    quota = YandexQuota(6)
    quota.begin_pass([], [])
    assert [quota.reserve_other() for _ in range(7)].count(Verdict.GRANTED) == 6

    reserved = YandexQuota(6)
    reserved.begin_pass([((EN_A, "en"), 2)], [((RU_REGISTRY, "ru"), 1)])
    granted = [reserved.reserve_other() for _ in range(6)]
    assert granted.count(Verdict.GRANTED) == 6 - ANALOG_WORLD_SLOTS - ANALOG_REGISTRY_SLOTS
    assert granted[-1] is Verdict.RESERVED


# ── Критерий 1а, 1б: предел без объявления прохода ──────────────────────────


@respx.mock
async def test_десять_параллельных_web_без_begin_pass_дают_ровно_шесть_запросов():
    route, flight = _mock_yandex()
    queries = [_q(f"параллельная формулировка {n}") for n in range(1, 11)]
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        await asyncio.gather(*(adapter.search(query, limit=5) for query in queries))

    assert route.call_count == 6, "параллельные формулировки перебрали платный предел"
    assert adapter.queries_used == 6
    assert flight.peak >= 2, "запросы шли гуськом: замок удерживается через сетевой вызов"
    problems = adapter.limit_problems
    assert len(problems) == 4
    skipped = [query.text for query in queries if not any(query.text in text for text in _sent(route))]
    assert len(skipped) == 4
    for text in skipped:
        assert sum(text in problem for problem in problems) == 1, f"«{text}» не названа в limit_problems"


@respx.mock
async def test_пять_web_и_два_английских_analog_без_begin_pass_дают_ровно_шесть():
    """Ровно сценарий ревью Э6-2в п. 4: на базе уходило 7 запросов."""
    route, _ = _mock_yandex()
    web = [_q(f"параллельная web {n}") for n in range(1, 6)]
    analog = [_q(EN_A, SourceType.ANALOG, "en"), _q(EN_B, SourceType.ANALOG, "en")]
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        await asyncio.gather(*(adapter.search(query, limit=5) for query in [*web, *analog]))

    assert route.call_count == 6
    assert adapter.queries_used == 6
    assert adapter.limit_problems, "вытесненная пачка аналогов не названа в limit_problems"


# ── Критерий 1в–1д: резерв через gather_candidates ──────────────────────────


@respx.mock
async def test_рынок_отдаёт_аналогам_шесть_минус_три_и_называет_вытесненное(settings):
    route, _ = _mock_yandex()
    plan = _market_plan()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        outcome = await _pipeline(settings, adapter).gather_candidates(plan)

    sent = _sent(route)
    assert route.call_count == 6 and adapter.queries_used == 6
    # Первые три прочие формулировки плана — в порядке плана, без приоритетов по типам.
    others = [text for text in sent if text.startswith(("веб формулировка", "конкурс формулировка"))]
    assert sorted(others) == ["веб формулировка 1", "веб формулировка 2", "веб формулировка 3"]
    # По одной пачке каждой английской analog и одна реестровая.
    for text, lang in ((EN_A, "en"), (EN_B, "en"), (RU_REGISTRY, "ru")):
        assert sent.count(plan_analog_queries(text, lang=lang).texts[0]) == 1
    assert plan_analog_queries(EN_A, lang="en").texts[1] not in sent
    assert plan_analog_queries(EN_B, lang="en").texts[1] not in sent

    problems = [p for p in outcome.problems if "yandex_search" in p]
    for text in ("веб формулировка 4", "веб формулировка 5", "конкурс формулировка 1", "конкурс формулировка 2"):
        mine = [p for p in problems if text in p]
        assert len(mine) == 1, f"«{text}» вытеснена, но не названа"
        assert "места оставлены аналогам" in mine[0] and "предел 6" in mine[0]
    for text in (EN_A, EN_B):
        second_batch = plan_analog_queries(text, lang="en").batches[1]
        mine = [p for p in problems if text in p]
        assert len(mine) == 1
        assert all(domain in mine[0] for domain in second_batch), "домены не опрошенной пачки не названы"
    assert len(problems) == 6


@respx.mock
async def test_без_русской_analog_прочим_достаётся_четыре_места(settings):
    route, _ = _mock_yandex()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        outcome = await _pipeline(settings, adapter).gather_candidates(_market_plan(ru=False))

    sent = _sent(route)
    assert route.call_count == 6
    assert len([t for t in sent if t.startswith(("веб формулировка", "конкурс формулировка"))]) == 4
    assert sum("места оставлены аналогам" in p for p in outcome.problems) == 3


@respx.mock
async def test_без_английских_analog_прочим_достаётся_пять_мест(settings):
    route, _ = _mock_yandex()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        outcome = await _pipeline(settings, adapter).gather_candidates(_market_plan(en=()))

    sent = _sent(route)
    assert route.call_count == 6
    assert len([t for t in sent if t.startswith(("веб формулировка", "конкурс формулировка"))]) == 5
    assert plan_analog_queries(RU_REGISTRY, lang="ru").texts[0] in sent
    assert sum("места оставлены аналогам" in p for p in outcome.problems) == 2


@respx.mock
async def test_без_analog_совсем_прочим_все_шесть_и_записей_о_резерве_нет(settings):
    route, _ = _mock_yandex()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        outcome = await _pipeline(settings, adapter).gather_candidates(_market_plan(en=(), ru=False))

    assert route.call_count == 6 and adapter.queries_used == 6
    assert len([p for p in outcome.problems if "yandex_search" in p]) == 1, "сверх шести остаётся один вытесненный"
    assert not any("оставлены аналогам" in p for p in outcome.problems)


@respx.mock
async def test_одна_английская_analog_получает_обе_пачки(settings):
    route, _ = _mock_yandex()
    plan = _market_plan(en=(EN_A,), ru=False)
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        outcome = await _pipeline(settings, adapter).gather_candidates(plan)

    sent = _sent(route)
    analog_texts = plan_analog_queries(EN_A, lang="en").texts
    assert all(text in sent for text in analog_texts), "единственная формулировка получает до двух пачек"
    assert route.call_count == 6
    assert not any(EN_A in p for p in outcome.problems), "обе пачки отправлены — записи о них быть не должно"


# ── Критерий 1е: «Наука» ────────────────────────────────────────────────────


@respx.mock
async def test_наука_analog_снят_кодом_и_все_шесть_мест_у_web(settings):
    route, _ = _mock_yandex()
    brief = Brief(industry="химия", task_summary="Регенерация катализатора.", keywords_ru=["катализатор"])
    assert brief.solution_kind == "research"
    raw = SearchPlan(
        queries=[
            *[_q(f"наука веб {n}") for n in range(1, 9)],
            _q(EN_A, SourceType.ANALOG, "en"),
            _q(EN_B, SourceType.ANALOG, "en"),
        ]
    )
    plan = normalize_plan_queries(raw, brief)
    assert not any(q.source_type is SourceType.ANALOG for q in plan.queries)
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        outcome = await _pipeline(settings, adapter).gather_candidates(plan)

    sent = _sent(route)
    assert route.call_count == 6 and adapter.queries_used == 6
    assert len([t for t in sent if t.startswith("наука веб")]) == 6
    assert not any(EN_A in t or EN_B in t for t in sent)
    assert not any("оставлены аналогам" in p for p in outcome.problems), "у «Науки» резерва нет"


# ── Критерий 1ж: два прохода ────────────────────────────────────────────────


@respx.mock
async def test_неиспользованные_места_первого_прохода_достаются_второму(settings):
    route, _ = _mock_yandex()
    first = _market_plan(web=2, contest=0)
    second = SearchPlan(queries=[_q(f"второй проход {n}") for n in range(1, 4)])
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        pipeline = _pipeline(settings, adapter)
        await pipeline.gather_candidates(first)
        assert adapter.queries_used == 5, "2 прочих + 2 мировых + 1 реестровая"
        outcome = await pipeline.gather_candidates(second)

    assert route.call_count == 6
    assert adapter.queries_used == 6, "повторный begin_pass не должен обнулять счёт"
    assert len(_other_sent(_sent(route), "второй проход")) == 1
    problems = [p for p in outcome.problems if "yandex_search" in p]
    assert len(problems) == 2
    assert all("второй проход" in p for p in problems)


# ── Инвариант 5: транспортный отказ — SearchError, место не возвращается ────


@respx.mock
async def test_оборванный_запрос_место_не_возвращает_и_остаётся_searcherror():
    from app.search.base import SearchError

    respx.post(SEARCH_ASYNC_URL).mock(side_effect=httpx.ConnectError("обрыв"))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        with pytest.raises(SearchError):
            await adapter.search(_q("обрыв транспорта"), limit=5)

    assert adapter.queries_used == 1, "оборванный запрос мог быть оплачен — место остаётся занятым"
    assert adapter.limit_problems == []


async def test_queries_used_присваивается_а_begin_pass_счёт_не_сбрасывает():
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client, max_queries=6)
        adapter.queries_used = 5
        assert adapter.queries_used == 5
        adapter.begin_pass([_q(EN_A, SourceType.ANALOG, "en")])
        assert adapter.queries_used == 5, "begin_pass не сбрасывает счёт прогона"
        assert adapter.limit_problems == []
