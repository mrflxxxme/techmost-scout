"""Частичный отказ платного источника виден клиенту (З-6, находка В-3 аудита Э2-Г).

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ
    На воротном прогоне Яндекс ответил 499 ровно на формулировку про грант —
    единственную про господдержку — и отдал 60 записей по другим формулировкам.
    `sources_unavailable` считает источник недоступным только если он не отдал
    вообще ничего, поэтому Яндекс в списке не оказался, а причина осталась
    только в `log.warning` (`orchestrator.py:511-513`), которого клиент не
    видит. Инвариант №5 требует обратного: скрытый отказ хуже видимого.

    Проверяется сквозной прогон через `ScoutPipeline.run`, а не только
    `gather_candidates`: урок Э2-В — ревью один раз вырезало целый блок кода, а
    модульные тесты остались зелёными, потому что не проверяли сборку прогона
    целиком.

Сеть не задействована: поисковый адаптер — синхронная заглушка (как в
tests/test_pipeline_counters.py и tests/test_pipeline_resilience.py), модель —
провайдер-заглушка, отвечающий по ключевой фразе в системном сообщении.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export.markdown import PAID_LIMIT_NOTICE, render_markdown
from app.llm import LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage
from app.models import Base, PipelineLog
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import Candidate, SourceType
from app.search.base import SearchAdapter

# Ключевые фразы из "## Роль" каждого промпта — тот же приём, что в
# tests/test_pipeline_resilience.py: провайдер выбирает ответ по ним, а не по
# порядку вызовов.
_PLANNER_KEY = "специалист по научно-техническому поиску"
_SCORER_KEY = "аналитик технологического скаутинга"
_CARD_AUTHOR_KEY = "технический писатель службы подбора"
_VERIFIER_KEY = "научный редактор-контролёр"
_SUMMARY_KEY = "руководитель проекта подбора"

# Формулировка, на которой источник отказывает, — намеренно про господдержку,
# как в реальном инциденте (грант на ультразвуковую диагностику супоросности).
_FAILING_QUERY_TEXT = "грант ультразвуковая диагностика супоросности свиноматок"
_OK_QUERY_TEXT = "ультразвуковая диагностика супоросности свиноматок"


class _ScriptedProvider(LLMProvider):
    """Провайдер-заглушка: ответ выбирается по ключевой фразе в системном
    сообщении (см. tests/test_pipeline_resilience.py)."""

    name = "deepseek"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    def __init__(self, routes: dict[str, list[str]]) -> None:
        self._routes = {keyword: list(responses) for keyword, responses in routes.items()}
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
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


class _PartialFailureAdapter(SearchAdapter):
    """Как Яндекс на воротном прогоне: одна формулировка падает транспортным
    отказом, вторая честно отвечает находкой."""

    name = "yandex_search"
    source_types = (SourceType.WEB,)

    async def search(self, query, *, limit: int = 20) -> list[Candidate]:
        if query.text == _FAILING_QUERY_TEXT:
            raise RuntimeError("499 Client Closed Request")
        return [
            Candidate(
                source_type=SourceType.WEB,
                title="Диагностика супоросности: обзор метода",
                url="https://example.org/ultrasound-diagnostics",
                org="НИИ ветеринарии",
                snippet="Ультразвуковой метод определения супоросности на ранних сроках.",
                provider="yandex_search",
            )
        ]


_PLAN_JSON_TWO = json.dumps(
    {
        "queries": [
            {
                "text": _OK_QUERY_TEXT,
                "source_type": "web",
                "lang": "ru",
                "rationale": "прямой поиск по задаче",
            },
            {
                "text": _FAILING_QUERY_TEXT,
                "source_type": "web",
                "lang": "ru",
                "rationale": "поиск господдержки",
            },
        ]
    },
    ensure_ascii=False,
)

_SCORE_JSON = json.dumps(
    {"scores": [{"index": 0, "relevance": 8, "maturity": 6, "accessibility": 7, "freshness": 6, "completeness": 7}]}
)

_CARD_JSON = json.dumps(
    {
        "title": "Ультразвуковая диагностика супоросности",
        "essence": "Метод ультразвуковой диагностики супоросности свиноматок на ранних сроках.",
        "maturity_stage": "pilot",
        "trl_estimate": None,
        "developer_org": "НИИ ветеринарии",
        "developer_team": None,
        "contact_leads": [],
        "evidence": [{"claim": "Метод описан в обзоре", "url": "https://example.org/ultrasound-diagnostics"}],
        "applicability": "Закрывает задачу ранней диагностики.",
        "risks": [],
        "gov_support": [],
        "next_step": "Связаться с НИИ ветеринарии.",
        "confidence": "medium",
        "confidence_note": "Один источник",
        "candidate_urls": ["https://example.org/ultrasound-diagnostics"],
        "requirement_checks": [],
        "unsourced_claims": [],
        "pdf_urls": [],
    },
    ensure_ascii=False,
)

_VERIFY_JSON = json.dumps(
    {
        "needs_rework": False,
        "confidence": "medium",
        "confidence_note": "подтверждения найдены",
        "unsourced_claims": [],
        "notes": ["ок"],
    },
    ensure_ascii=False,
)

_SUMMARY_JSON = json.dumps({"comparison_rows": [], "recommendation": "Рассмотреть вариант №1.", "caveats": []})


def _pipeline(settings, provider: LLMProvider, adapters: list[SearchAdapter], *, session_factory=None) -> ScoutPipeline:
    router = LLMRouter(settings, providers={"deepseek": provider})
    return ScoutPipeline(settings=settings, router=router, adapters=adapters, session_factory=session_factory)


@pytest.fixture
async def db_session_factory(tmp_path):
    """Фабрика сессий на временном файле SQLite (как в
    tests/test_pipeline_resilience.py) — нужна только критерию 3: журнал стадий
    `run()` живёт внутри `_run_stages` и наружу не отдаётся, единственный
    способ прочитать запись стадии `search` — через запись в БД."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial-failure-test.db'}", future=True)
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


def _scripted_provider() -> _ScriptedProvider:
    return _ScriptedProvider(
        {
            _PLANNER_KEY: [_PLAN_JSON_TWO],
            _SCORER_KEY: [_SCORE_JSON],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )


# ── Критерий 1: частичный отказ доезжает до клиента ─────────────────────────
async def test_частичный_отказ_доезжает_до_клиента(settings, brief):
    """Источник отвечает отказом на одну формулировку из двух и нормально на
    вторую: в собранном отчёте есть блок «Ограничения этого поиска» с
    одобренной строкой, при этом `sources_unavailable` пуст."""
    provider = _scripted_provider()
    async with httpx.AsyncClient() as client:
        report = await _pipeline(settings, provider, [_PartialFailureAdapter(client)]).run(brief, request_id=1)

    assert report.sources_unavailable == [], "источник ответил на одну из двух формулировок — недоступным не считается"
    assert PAID_LIMIT_NOTICE in report.summary.caveats

    markdown = render_markdown(report)
    assert "## Ограничения этого поиска" in markdown
    assert PAID_LIMIT_NOTICE in markdown


# ── Критерий 2: оговорка не задваивается ────────────────────────────────────
async def test_оговорка_не_задваивается_при_обоих_признаках(settings, brief):
    """Пропуск по платному пределу и частичный отказ сработали вместе — строка
    оговорки встречается в caveats ровно один раз."""
    provider = _scripted_provider()
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_PartialFailureAdapter(client)])
        # Симулируем, что тот же прогон исчерпал платный предел (второй,
        # независимый признак того же класса — РЕШ-127 п. 2).
        pipeline._limit_skipped_total = 1
        report = await pipeline.run(brief, request_id=1)

    occurrences = [caveat for caveat in report.summary.caveats if caveat == PAID_LIMIT_NOTICE]
    assert len(occurrences) == 1, "оговорка добавлена дважды — по одному разу на каждый признак"


def test_признак_оговорки_переживает_подмену_текста_константы(settings, brief, monkeypatch):
    """Мутация (д) из PLAN.md: опознание оговорки не должно держаться на
    подстроке текста — счётчик решает добавлять её независимо от содержимого
    константы `PAID_LIMIT_NOTICE` (приём РЕШ-129)."""
    import app.pipeline.orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "PAID_LIMIT_NOTICE", "другой текст оговорки")

    async def scenario() -> None:
        provider = _scripted_provider()
        async with httpx.AsyncClient() as client:
            report = await _pipeline(settings, provider, [_PartialFailureAdapter(client)]).run(brief, request_id=1)
        occurrences = [caveat for caveat in report.summary.caveats if caveat == "другой текст оговорки"]
        assert len(occurrences) == 1

    import asyncio

    asyncio.run(scenario())


# ── Критерий 3: причина отказа доезжает до журнала стадии ───────────────────
async def test_причина_отказа_в_журнале_стадии_search(settings, brief, db_session_factory):
    """Запись `search` в `pipeline_logs` несёт непустой `error` с именем
    источника и текстом упавшей формулировки — иначе разбор инцидента снова
    упирался бы в лог, которого снаружи не видно. Журнал стадий живёт внутри
    `_run_stages` и наружу не отдаётся, поэтому читаем его из БД, как и
    tests/test_pipeline_resilience.py."""
    provider = _scripted_provider()
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_PartialFailureAdapter(client)], session_factory=db_session_factory)
        await pipeline.run(brief, request_id=1)

    async with db_session_factory() as session:
        rows = (await session.execute(select(PipelineLog).where(PipelineLog.stage == "search"))).scalars().all()

    assert rows, "запись стадии search не найдена в журнале"
    record = rows[0]
    assert record.error, "причина отказа обязана попасть в error записи стадии search"
    assert "yandex_search" in record.error
    assert _FAILING_QUERY_TEXT in record.error


# ── Граница задачи: клиентский текст не менялся ──────────────────────────────
def test_текст_оговорки_совпадает_с_одобренной_редакцией_рош_129():
    """Контроль границы задачи: константа не тронута — сверка буквального
    текста, утверждённого владельцем (РЕШ-129)."""
    assert PAID_LIMIT_NOTICE == (
        "Часть поисковых формулировок не была выполнена — выдача по этой теме может быть неполной."
    )
