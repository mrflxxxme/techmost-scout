"""Учёт прогона: недобор по платному пределу и проход стадии — в журнале (Т-37).

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ
    Первый цикл правок пост-аудита Э2-Б завёл обе сущности и не подключил ни
    одну. Записи о пропуске по пределу лежали в адаптере Яндекса, `pass_no`
    стоял в `StageRecord`, — а прогон не звал `drain_limit_problems` ни разу и
    не ставил двойку ни в одну запись второго прохода. Существующие тесты этого
    не увидели, потому что сторожили трубу, а не течение:
    `tests/test_yandex_limit_visibility.py` дёргает адаптер напрямую, а
    `tests/test_stage_pass_no.py` сам конструирует запись с двойкой и отдаёт её
    `persist_run`.

    Здесь всё проверяется ТОЛЬКО через прогон `ScoutPipeline.run`: что
    записалось в `pipeline_logs`, то и доехало. Мутация проводки — убрать вызов
    `drain_limit_problems` из `gather_candidates` либо снять `pass_no` со стадий
    второго прохода — обязана ронять тесты этого файла.

Сети нет (инвариант №7): Яндекс подменён через respx, модель — провайдер-заглушка,
база — временный SQLite в `tmp_path`.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.llm import LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage
from app.models import Base, PipelineLog
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import Candidate, SearchPlan, SearchQuery, SourceType
from app.search.base import SearchAdapter
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter

# ── Общая оснастка ──────────────────────────────────────────────────────────


@pytest.fixture
async def db_session_factory(tmp_path):
    """Фабрика сессий на временном SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'run-accounting.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    yield factory
    await engine.dispose()


async def _stage_rows(session_factory) -> list[PipelineLog]:
    """Строки журнала стадий в порядке записи."""
    async with session_factory() as session:
        return list((await session.execute(select(PipelineLog).order_by(PipelineLog.id))).scalars().all())


# ── Ц-1: пропуск по платному пределу доезжает до журнала прогона ────────────

_WEB_PLAN = SearchPlan(
    queries=[
        SearchQuery(
            text="катализатор гидроочистки регенерация",
            source_type=SourceType.WEB,
            lang="ru",
            rationale="первая формулировка плана",
        ),
        SearchQuery(
            text="hydrotreating catalyst regeneration additive",
            source_type=SourceType.WEB,
            lang="en",
            rationale="вторая формулировка плана",
        ),
    ]
)

_EMPTY_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    "<yandexsearch version='1.0'><response><results><grouping><group>"
    "</group></grouping></results></response></yandexsearch>"
)


class _MuteProvider(LLMProvider):
    """Провайдер, который обязан остаться неспрошенным.

    Прогон по готовому плану без единого кандидата модель не зовёт вовсе, и это
    не деталь оснастки: обращение к модели здесь означало бы, что тест меряет не
    тот путь.
    """

    name = "deepseek"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    async def complete(self, messages: list[Message], **kwargs) -> LLMResult:
        raise AssertionError("прогон по готовому плану с пустой выдачей не имеет права звать модель")

    async def health(self) -> tuple[bool, str]:
        return True, "заглушка"


def _yandex(client: httpx.AsyncClient, *, max_queries: int) -> YandexSearchAdapter:
    """Настоящий адаптер Яндекса с фиктивным ключом (инвариант №3)."""
    return YandexSearchAdapter(
        client,
        api_key="test-key",
        folder_id="b1gtest",
        max_queries=max_queries,
        poll_interval_seconds=0.0,
        max_wait_seconds=5.0,
    )


async def _run_web_search(settings, brief, session_factory, *, max_queries: int):
    """Прогон по готовому плану из двух web-формулировок с одним источником."""
    router = LLMRouter(settings, providers={"deepseek": _MuteProvider()})
    async with httpx.AsyncClient() as client:
        adapter = _yandex(client, max_queries=max_queries)
        pipeline = ScoutPipeline(
            settings=settings,
            router=router,
            adapters=[adapter],
            session_factory=session_factory,
            preset_plan=_WEB_PLAN,
        )
        report = await pipeline.run(brief, request_id=1, raw_text="задача теста")
    return report, adapter


@respx.mock
async def test_исчерпанный_предел_яндекса_виден_в_журнале_прогона(settings, brief, db_session_factory):
    """Пропуск по пределу доезжает от адаптера до записи стадии `search`.

    Это и есть проводка, которой не было: `drain_limit_problems` существовал, но
    оркестратор его не звал, и наружу не выходило ничего. Проверяется конец
    пути — строка `pipeline_logs`, а не список внутри адаптера: до отчёта о
    расходе доживает только записанное.
    """
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    report, adapter = await _run_web_search(settings, brief, db_session_factory, max_queries=0)

    assert not route.called, "предел исчерпан — ни одного платного обращения быть не должно"
    assert adapter.queries_used == 0
    assert report.sources_unavailable == [], "исчерпан наш бюджет, а не источник: он в недоступные не уезжает"

    search_rows = [row for row in await _stage_rows(db_session_factory) if row.stage == "search"]
    assert len(search_rows) == 1
    error = search_rows[0].error
    assert error, "исчерпанный платный предел снова не доехал до журнала — вызова drain_limit_problems нет"
    assert "yandex_search" in error, "по записи не понять, у какого источника кончился предел"
    assert "предел" in error and "НЕ задавалась" in error, "в журнале нет причины недобора"
    for query in _WEB_PLAN.queries:
        assert query.text in error, f"формулировка «{query.text}» не названа — недобор нечем измерить"


@respx.mock
async def test_пустая_выдача_источника_оставляет_запись_стадии_чистой(settings, brief, db_session_factory):
    """Контроль отличимости: источник спросили, он ответил, находок нет.

    Без этой пары первый тест ничего не доказывает — запись «предел исчерпан»
    имеет смысл ровно постольку, поскольку при обычной пустой выдаче её нет.
    Именно эта неотличимость и была находкой Ц-1.
    """
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"done": True, "response": {"rawData": base64.b64encode(_EMPTY_XML.encode()).decode("ascii")}},
        )
    )
    report, adapter = await _run_web_search(settings, brief, db_session_factory, max_queries=6)

    assert adapter.queries_used == 2, "обе формулировки обязаны быть оплачены и отправлены"
    assert report.cards == []
    search_rows = [row for row in await _stage_rows(db_session_factory) if row.stage == "search"]
    assert len(search_rows) == 1
    assert search_rows[0].error is None, (
        "пустая выдача записана как проблема — записи о пределе и о нулевой находке снова неотличимы"
    )


# ── Ц-2: стадии второго прохода помечены вторым проходом ────────────────────

#: Ключевые фразы из "## Роль" промптов — по ним заглушка узнаёт стадию.
_REPLAN_KEY = "аналитик повторного поиска"
_SCORER_KEY = "аналитик технологического скаутинга"
_CARD_AUTHOR_KEY = "технический писатель службы подбора"
_VERIFIER_KEY = "научный редактор-контролёр"
_SUMMARY_KEY = "руководитель проекта подбора"

#: Пометка в тексте формулировки, по которой поддельный источник узнаёт запрос
#: ВТОРОГО плана: выдача второго прохода обязана отличаться от первой.
_SECOND_PASS_MARK = "повторно"


class _ScriptedProvider(LLMProvider):
    """Ответ выбирается по ключевой фразе системного сообщения; ответы на одну
    фразу выдаются по очереди, последний — повторно."""

    name = "deepseek"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    def __init__(self, routes: dict[str, list[str]]) -> None:
        self._routes = {keyword: list(responses) for keyword, responses in routes.items()}
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message], **kwargs) -> LLMResult:
        self.calls.append(messages)
        system_text = messages[0].content if messages else ""
        for keyword, queue in self._routes.items():
            if keyword not in system_text:
                continue
            if not queue:
                raise LLMError(self.name, f"заготовленные ответы для «{keyword}» закончились", retryable=False)
            text = queue.pop(0) if len(queue) > 1 else queue[0]
            return LLMResult(
                text=text,
                usage=Usage(tokens_in=10, tokens_out=10, cost_rub=0.001, provider=self.name, model="fake"),
            )
        raise AssertionError(f"тест не подготовил ответ для запроса: {system_text[:120]}")

    async def health(self) -> tuple[bool, str]:
        return True, "заглушка"


def _candidate(number: int) -> Candidate:
    """Кандидат с DOI в `raw_id`: по нему стадия обогащения находит, что просить."""
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=f"Кандидат {number}",
        url=f"https://doi.org/10.1/c{number}",
        org="Институт катализа",
        year=2025,
        snippet="Фрагмент описания кандидата.",
        raw_id=f"10.1/c{number}",
        provider="crossref",
    )


class _TwoPassAdapter(SearchAdapter):
    """Источник, чья выдача зависит от того, чей план его спрашивает, и умеющий
    обогащать по DOI.

    Обогащение здесь отказывает нарочно: отказ — единственный способ заставить
    стадию `enrich` оставить строку в журнале, а строка нужна, чтобы проверить
    её номер прохода. Отказ обогащения прогон не роняет (инвариант №5).
    """

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    def __init__(self, client: httpx.AsyncClient, *, first: list[int], second: list[int]) -> None:
        super().__init__(client)
        self._first = first
        self._second = second

    async def search(self, query, *, limit: int = 20) -> list[Candidate]:
        numbers = self._second if _SECOND_PASS_MARK in query.text else self._first
        return [_candidate(number) for number in numbers]

    async def enrich_by_doi(self, dois: list[str]) -> dict[str, dict]:
        raise RuntimeError("источник обогащения ответил 429")


def _plan_json(text: str) -> str:
    return json.dumps(
        {"queries": [{"text": text, "source_type": "publication", "lang": "ru", "rationale": "формулировка теста"}]},
        ensure_ascii=False,
    )


def _scores_json(count: int, relevance: int) -> str:
    return json.dumps(
        {
            "scores": [
                {
                    "index": index,
                    "relevance": relevance,
                    "maturity": 5,
                    "accessibility": 5,
                    "freshness": 5,
                    "completeness": 5,
                    "notes": "оценка теста",
                }
                for index in range(count)
            ]
        },
        ensure_ascii=False,
    )


_CARD_JSON = json.dumps(
    {
        "title": "Катализатор с присадками",
        "essence": "Присадка продлевает срок службы катализатора на сернистом сырье.",
        "maturity_stage": "pilot",
        "trl_estimate": None,
        "developer_org": "Институт катализа",
        "developer_team": None,
        "contact_leads": [],
        "evidence": [{"claim": "Испытан на установке", "url": "https://doi.org/10.1/c1"}],
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "risks": [],
        "gov_support": [],
        "next_step": "Запросить протокол испытаний.",
        "confidence": "medium",
        "confidence_note": "Один источник",
        "candidate_urls": ["https://doi.org/10.1/c1"],
        "requirement_checks": [],
        "unsourced_claims": [],
        "pdf_urls": [],
    },
    ensure_ascii=False,
)

_VERIFY_JSON = json.dumps(
    {"needs_rework": False, "confidence": "medium", "confidence_note": "ок", "unsourced_claims": [], "notes": "ок"},
    ensure_ascii=False,
)

_SUMMARY_JSON = json.dumps(
    {"comparison_rows": [], "recommendation": "Рассмотреть вариант №1.", "caveats": []}, ensure_ascii=False
)

#: План первого прохода задаётся готовым (`preset_plan`), поэтому планировщика в
#: заготовках нет: стадия `planner` всё равно пишет свою строку журнала, и её
#: номер прохода проверяется наравне с остальными.
_PRESET_FIRST_PLAN = SearchPlan(
    queries=[
        SearchQuery(
            text="катализатор гидроочистки сернистое сырьё",
            source_type=SourceType.PUBLICATION,
            lang="ru",
            rationale="формулировка первого плана",
        )
    ]
)


async def _run_two_passes(settings, brief, session_factory):
    """Прогон с гарантированным вторым проходом: максимум relevance 4 при пороге 6."""
    provider = _ScriptedProvider(
        {
            _REPLAN_KEY: [_plan_json(f"регенерация катализатора {_SECOND_PASS_MARK} другими словами")],
            _SCORER_KEY: [_scores_json(10, 4), _scores_json(4, 8)],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    tuned = settings.model_copy(update={"second_pass_relevance_max": 6})
    router = LLMRouter(tuned, providers={"deepseek": provider})
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(
            settings=tuned,
            router=router,
            adapters=[
                _TwoPassAdapter(client, first=list(range(1, 11)), second=[1, 2, 3, 4, 5, 6, 11, 12, 13, 14]),
            ],
            session_factory=session_factory,
            preset_plan=_PRESET_FIRST_PLAN,
        )
        report = await pipeline.run(brief, request_id=1, raw_text="задача теста")
    return report


async def test_стадии_второго_прохода_помечены_вторым_проходом_в_прогоне(settings, brief, db_session_factory):
    """После прогона со вторым проходом в журнале есть строки с `pass_no = 2`,
    и это ровно `replan`, второй `search`, второе обогащение и второй `scorer`.

    Проверка идёт по журналу настоящего прогона, а не по записи, которую тест
    сам сконструировал: заполненное всегда единицей поле выглядело работающим
    ровно потому, что его сторожил только `persist_run`.
    """
    report = await _run_two_passes(settings, brief, db_session_factory)
    assert report.cards, "прогон обязан дойти до карточек, иначе меряется не второй проход"

    rows = await _stage_rows(db_session_factory)
    assert [row.stage for row in rows].count("replan") == 1, "второго прохода не случилось — проверять нечего"

    second = [row.stage for row in rows if row.pass_no == 2]
    assert second == ["replan", "search", "enrich", "scorer"], (
        f"стадии второго прохода помечены не вторым проходом: в журнале {second}"
    )
    first = [row.stage for row in rows if row.pass_no == 1]
    assert first.count("search") == 1 and first.count("scorer") == 1, (
        f"строка второго прохода приписана первому: {first}"
    )
    assert {"planner", "cards", "summary"} <= set(first), "однопроходные стадии обязаны остаться первым проходом"


async def test_расход_второго_прохода_считается_фильтром_по_проходу(settings, brief, db_session_factory):
    """Доклад владельцу о расходе второго прохода (РЕШ-82) — сумма по `pass_no == 2`.

    Именно этот запрос и возвращал ноль: поле стояло, двойки в нём не было, и
    трата второго прохода приписывалась первому. Ноль здесь — провал, а не
    «второй проход обошёлся даром».
    """
    await _run_two_passes(settings, brief, db_session_factory)
    rows = await _stage_rows(db_session_factory)

    spent_second = sum(row.cost_rub for row in rows if row.pass_no == 2)
    spent_first = sum(row.cost_rub for row in rows if row.pass_no == 1)
    assert spent_second > 0, "расход второго прохода по фильтру `pass_no == 2` равен нулю — поле не заполняется"
    assert spent_first > 0, "первый проход тоже обязан быть виден: разделение, а не перенос всего во второй"


async def test_обогащение_второго_прохода_не_приписано_первому(settings, brief, db_session_factory):
    """У стадии `enrich` два вызова за прогон — по одному на проход, и оба с
    непустым `error` (обогащение в тесте отказывает).

    Отдельная проверка, потому что обогащение зовётся не из `_second_pass`
    напрямую, а общим методом с умолчанием `pass_no = 1`: забытый аргумент
    отправил бы отказ обогащения второго прохода в отчёт о первом.
    """
    await _run_two_passes(settings, brief, db_session_factory)
    enrich_rows = [row for row in await _stage_rows(db_session_factory) if row.stage == "enrich"]

    assert [row.pass_no for row in enrich_rows] == [1, 2], (
        f"проходы обогащения в журнале неразличимы: {[row.pass_no for row in enrich_rows]}"
    )
    assert all(row.error for row in enrich_rows), "тест обязан идти по настоящему отказу обогащения, иначе он слеп"
