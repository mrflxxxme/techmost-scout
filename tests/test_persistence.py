"""Минимальная запись прогона в БД (Ф-8, РЕШ-40) и стоп-кран лимита (РЕШ-45).

Проверяем: успешный прогон кладёт по строке в `requests` (статус `drafted`,
стоимость совпадает с `report.cost_rub`), `solution_cards` (с сохранённым
`evidence`) и `pipeline_logs` (не менее одной записи на стадию поиска с
непустым `sources_hit`); без переданной фабрики сессий прогон отрабатывает
как раньше и в БД не пишет; отказ самой записи (фабрика бросает исключение)
не роняет прогон; падение стадии — статус `failed` и текст ошибки в `requests`.

Отдельно (РЕШ-45, пост-аудит фазы): исчерпание лимита стоимости запроса
(`CostLimitExceededError`) — не отказ модели, а команда «стоп тратить», и до
правки она гасилась тремя широкими `except Exception` (батч скоринга, сборка
карточки, сводка) — прогон тихо деградировал вместо остановки. Здесь
проверяется, что во всех трёх местах исключение доходит до `pipeline.run()`
нетронутым и в БД остаётся статус `failed` — тот же путь `persist_run` на
необработанном исключении, что и в падении обычной стадии выше.

База — временный файл SQLite (`tmp_path`), сеть не задействована: провайдер
модели и поисковый адаптер — заглушки, как в tests/test_pipeline_resilience.py
и tests/test_pipeline_counters.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.llm import CostLimitExceededError, LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message
from app.models import Base, PipelineLog
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import Candidate, SourceType
from app.search.base import SearchAdapter


class _OneHitAdapter(SearchAdapter):
    """Один рабочий источник, одна публикация на любую формулировку."""

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query, *, limit: int = 20) -> list[Candidate]:
        return [
            Candidate(
                source_type=SourceType.PUBLICATION,
                title="Катализатор с присадками для сероустойчивости",
                url="https://doi.org/10.1/test-persist",
                org="Институт катализа",
                year=2025,
                snippet="Испытан на действующей установке гидроочистки.",
                raw_id="10.1/test-persist",
                provider="crossref",
            )
        ]


class _AlwaysFailProvider(LLMProvider):
    """Провайдер, который всегда отказывает без переспроса (не retryable) —
    моделирует падение стадии без секундных задержек на повторы роутера."""

    name = "deepseek"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
        raise LLMError(self.name, "провайдер настроен на отказ для теста", retryable=False)

    async def health(self) -> tuple[bool, str]:
        return False, "заглушка отказа"


_PLAN_JSON = json.dumps(
    {
        "queries": [
            {
                "text": "катализатор гидроочистки серостойкий",
                "source_type": "publication",
                "lang": "ru",
                "rationale": "прямой поиск по задаче",
            }
        ]
    },
    ensure_ascii=False,
)

_SCORE_JSON = json.dumps(
    {"scores": [{"index": 0, "relevance": 8, "maturity": 6, "accessibility": 7, "freshness": 6, "completeness": 7}]}
)

_CARD_JSON = json.dumps(
    {
        "title": "Катализатор с присадками для сероустойчивости",
        "essence": "Катализатор гидроочистки с присадкой, продлевающей срок службы на сырье с серой.",
        "maturity_stage": "pilot",
        "trl_estimate": None,
        "developer_org": "Институт катализа",
        "developer_team": None,
        "contact_leads": [],
        "evidence": [
            {"claim": "Испытан на действующей установке гидроочистки", "url": "https://doi.org/10.1/test-persist"}
        ],
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "risks": [],
        "gov_support": [],
        "next_step": "Запросить у Института катализа протокол испытаний.",
        "confidence": "medium",
        "confidence_note": "Один источник",
        "candidate_urls": ["https://doi.org/10.1/test-persist"],
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
        "notes": "ок",
    },
    ensure_ascii=False,
)

_SUMMARY_JSON = json.dumps({"comparison_rows": [], "recommendation": "Рассмотреть вариант №1.", "caveats": []})


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — та же форма, что у
    `app.db.session_scope`, но без общего движка приложения (правило теста:
    временная база через tmp_path, а не боевой `data/scout.db`)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'persist-test.db'}", future=True)
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


def _pipeline(settings, provider: LLMProvider, adapters: list[SearchAdapter], *, session_factory=None) -> ScoutPipeline:
    router = LLMRouter(settings, providers={"deepseek": provider})
    return ScoutPipeline(settings=settings, router=router, adapters=adapters, session_factory=session_factory)


# ── Критерий 1: успешный прогон пишет requests, solution_cards, pipeline_logs
async def test_successful_run_persists_request_card_and_search_log(
    settings, brief, fake_provider_factory, db_session_factory
):
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_OneHitAdapter(client)], session_factory=db_session_factory)
        report = await pipeline.run(brief, request_id=1, raw_text="сырой текст задачи про катализатор")

    assert len(report.cards) == 1

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1
        assert requests[0].status == "drafted"
        assert requests[0].raw_text == "сырой текст задачи про катализатор"
        # РЕШ-40: cost_rub в requests совпадает с тем, что печатает CLI из report.cost_rub.
        assert requests[0].cost_rub == report.cost_rub

        cards = (await session.execute(select(SolutionCardRow))).scalars().all()
        assert len(cards) == 1
        assert cards[0].request_id == requests[0].id
        assert cards[0].evidence  # непустой сохранённый evidence

        search_logs = (await session.execute(select(PipelineLog).where(PipelineLog.stage == "search"))).scalars().all()
        assert len(search_logs) >= 1
        assert search_logs[0].sources_hit  # непустой словарь по источникам


# ── Критерий 2: без фабрики прогон отрабатывает и в БД не пишет ─────────────
async def test_without_factory_pipeline_runs_and_writes_nothing(settings, brief, fake_provider_factory, tmp_path):
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_OneHitAdapter(client)])  # session_factory не передана
        report = await pipeline.run(brief, request_id=1, raw_text="текст без записи в БД")

    assert len(report.cards) == 1
    # Фабрика не передана — до файла базы дело вообще не доходит.
    assert not (tmp_path / "persist-test.db").exists()


# ── Критерий 3: отказ фабрики при записи не роняет прогон ───────────────────
async def test_broken_factory_does_not_crash_run(settings, brief, fake_provider_factory):
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])

    def broken_factory():
        raise RuntimeError("база залокирована другим процессом")

    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_OneHitAdapter(client)], session_factory=broken_factory)
        report = await pipeline.run(brief, request_id=1, raw_text="текст при сломанной фабрике")

    assert len(report.cards) == 1
    assert report.summary.recommendation


# ── Падение прогона: статус failed и текст ошибки в requests ────────────────
async def test_pipeline_failure_persists_failed_status_and_error(settings, brief, db_session_factory):
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(
            settings, _AlwaysFailProvider(), [_OneHitAdapter(client)], session_factory=db_session_factory
        )
        with pytest.raises(LLMError):
            await pipeline.run(brief, request_id=1, raw_text="текст, который не соберётся")

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1
        assert requests[0].status == "failed"
        assert requests[0].error
        assert requests[0].raw_text == "текст, который не соберётся"


# ── РЕШ-45: стоп-кран лимита стоимости роняет прогон, а не деградирует ──────
#
# FakeProvider (conftest.py) отдаёт фиксированную Usage(cost_rub=0.01) на
# каждый успешный вызов независимо от стадии. Подбирая
# `cost_limit_rub_per_request` так, чтобы он укладывался в стоимость нужного
# числа вызовов, но не следующего, лимит исчерпывается ровно на проверяемой
# стадии — раньше стадии отказать нечем.


async def test_cost_limit_exceeded_during_scoring_raises_and_persists_failed(
    settings, brief, fake_provider_factory, db_session_factory
):
    """Лимит исчерпан на батче скоринга: план (0.01 ₽) укладывается в 0.015 ₽,
    скоринг (ещё 0.01 ₽ → 0.02 ₽) — нет. Раньше стадия ловила исключение и
    подставляла нейтральные оценки (stages.py, `except Exception` до правки);
    теперь `CostLimitExceededError` доходит до `pipeline.run()` нетронутым, и
    в БД остаётся статус `failed` — тем же путём, что и падение любой стадии
    (persist_run на необработанном исключении)."""
    low_limit = settings.model_copy(update={"cost_limit_rub_per_request": 0.015})
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(low_limit, provider, [_OneHitAdapter(client)], session_factory=db_session_factory)
        with pytest.raises(CostLimitExceededError):
            await pipeline.run(brief, request_id=1, raw_text="текст, который упрётся в лимит на скоринге")

    # План + один батч скоринга — ни карточка, ни сводка не вызывались.
    assert len(provider.calls) == 2

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1
        assert requests[0].status == "failed"
        assert "лимит" in requests[0].error


async def test_cost_limit_exceeded_during_card_authoring_raises_and_persists_failed(
    settings, brief, fake_provider_factory, db_session_factory
):
    """Лимит исчерпан на сборке карточки: план + скоринг (0.02 ₽) укладываются
    в 0.025 ₽, сборка карточки (ещё 0.01 ₽ → 0.03 ₽) — нет. Раньше
    `orchestrator._author_cards.one()` ловил исключение и тихо терял карточку
    (`return None`); теперь исключение пробрасывается и роняет весь прогон —
    молчаливая потеря одной карточки позволила бы остальным группам (при их
    наличии) продолжать платить дальше."""
    low_limit = settings.model_copy(update={"cost_limit_rub_per_request": 0.025})
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(low_limit, provider, [_OneHitAdapter(client)], session_factory=db_session_factory)
        with pytest.raises(CostLimitExceededError):
            await pipeline.run(brief, request_id=1, raw_text="текст, который упрётся в лимит на карточке")

    # План, скоринг, сборка карточки — проверка карточки и сводка не дошли.
    assert len(provider.calls) == 3

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1
        assert requests[0].status == "failed"
        assert "лимит" in requests[0].error


async def test_cost_limit_exceeded_during_summary_raises_and_persists_failed(
    settings, brief, fake_provider_factory, db_session_factory
):
    """Лимит исчерпан на сводке: план + скоринг + карточка + проверка
    (0.04 ₽) укладываются в 0.045 ₽, сводка (ещё 0.01 ₽ → 0.05 ₽) — нет.
    Раньше отказ стадии summary ловился как отказ модели и подменялся
    запасной сводкой (`stages.fallback_summary`, без вызова модели) — прогон
    с уже готовой карточкой не падал. Лимит — не отказ модели, а команда
    «стоп тратить»: запасная сводка НЕ строится, прогон падает целиком."""
    low_limit = settings.model_copy(update={"cost_limit_rub_per_request": 0.045})
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(low_limit, provider, [_OneHitAdapter(client)], session_factory=db_session_factory)
        with pytest.raises(CostLimitExceededError):
            await pipeline.run(brief, request_id=1, raw_text="текст, который упрётся в лимит на сводке")

    # Все пять вызовов стадий состоялись — отказал ровно последний (summary),
    # и после него новых вызовов не было (запасная сводка модель не зовёт).
    assert len(provider.calls) == 5

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1
        assert requests[0].status == "failed"
        assert "лимит" in requests[0].error
