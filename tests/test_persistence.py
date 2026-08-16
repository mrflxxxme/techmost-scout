"""Минимальная запись прогона в БД (Ф-8, РЕШ-40) и стоп-кран лимита (РЕШ-45).

Проверяем: успешный прогон кладёт по строке в `requests` (статус `drafted`,
стоимость совпадает с `report.cost_rub`), `solution_cards` (с сохранённым
`evidence`) и `pipeline_logs` (не менее одной записи на стадию поиска с
непустым `sources_hit`); без переданной фабрики сессий прогон отрабатывает
как раньше и в БД не пишет; отказ самой записи (фабрика бросает исключение)
не роняет прогон; падение стадии — статус `failed` и текст ошибки в `requests`.

Отдельно (Т-5, правка П-А): три поля карточки, появившиеся в фазе Э1-2-Ф —
`requirement_checks`, `unsourced_claims`, `pdf_urls` — до правки в БД не
попадали, и карточка в базе выходила «увереннее» своей markdown-версии
(инвариант №1). Здесь проверяется, что они доезжают до `solution_cards` без
потерь и что карточка без них даёт пустые списки, а не NULL.

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
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export.markdown import render_markdown
from app.llm import CostLimitExceededError, LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message
from app.models import Base, PipelineLog
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.pipeline.orchestrator import ScoutPipeline
from app.pipeline.persistence import persist_run
from app.schemas import Candidate, Confidence, Evidence, MaturityStage, RequirementCheck, SolutionCard, SourceType
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


# ── Т-5: три поля фазы Э1-2-Ф доезжают до solution_cards ────────────────────
#
# Пишем через `persist_run` напрямую: поля заполняются на разных стадиях
# (сверка — автором карточки, `unsourced_claims` — проверяющей стадией,
# `pdf_urls` — кодом обогащения), и собирать их все через полный прогон значило
# бы проверять оркестратор, а не запись в БД.


def _card(**overrides) -> SolutionCard:
    """Минимально валидная карточка; именованные аргументы перекрывают поля."""
    fields = {
        "title": "Катализатор с присадками для сероустойчивости",
        "essence": "Присадка продлевает межрегенерационный цикл на сырье с серой.",
        "applicability": "Закрывает задачу продления цикла.",
        "next_step": "Запросить протокол испытаний.",
        "evidence": [
            Evidence(claim="Испытан на действующей установке", url="https://doi.org/10.1/test-persist"),
        ],
        "maturity_stage": MaturityStage.PILOT,
        "confidence": Confidence.MEDIUM,
    }
    fields.update(overrides)
    return SolutionCard(**fields)


async def test_new_card_fields_persist_without_loss(db_session_factory):
    """Сверка с требованиями, неподтверждённые утверждения и ссылки на PDF
    читаются из базы ровно теми, что печатает markdown-отчёт (инвариант №1)."""
    card = _card(
        requirement_checks=[
            RequirementCheck(
                requirement="Стойкость к сере не ниже 2%",
                status="confirmed",
                evidence_url="https://doi.org/10.1/test-persist",
                note="прямо указано в аннотации",
            ),
            RequirementCheck(requirement="Срок службы 5 лет", status="not_confirmed", note="в источниках не найдено"),
        ],
        unsourced_claims=["Снижение затрат на 30% источником не подтверждено"],
        pdf_urls=["https://arxiv.org/pdf/2501.00001"],
    )

    await persist_run(
        db_session_factory,
        raw_text="текст задачи с новыми полями карточки",
        status="drafted",
        cost_rub=0.0,
        cards=[card],
        stages=[],
    )

    async with db_session_factory() as session:
        row = (await session.execute(select(SolutionCardRow))).scalars().one()

    assert row.requirement_checks == [
        {
            "requirement": "Стойкость к сере не ниже 2%",
            "status": "confirmed",
            "evidence_url": "https://doi.org/10.1/test-persist",
            "note": "прямо указано в аннотации",
        },
        {
            "requirement": "Срок службы 5 лет",
            "status": "not_confirmed",
            "evidence_url": None,
            "note": "в источниках не найдено",
        },
    ]
    assert row.unsourced_claims == ["Снижение затрат на 30% источником не подтверждено"]
    assert row.pdf_urls == ["https://arxiv.org/pdf/2501.00001"]


async def test_card_without_new_fields_persists_empty_lists_not_null(db_session_factory):
    """Карточка без новых полей даёт пустые списки, а не NULL: иначе чтение
    обратно пришлось бы всюду страховать проверкой на None."""
    await persist_run(
        db_session_factory,
        raw_text="текст задачи без новых полей карточки",
        status="drafted",
        cost_rub=0.0,
        cards=[_card()],
        stages=[],
    )

    async with db_session_factory() as session:
        row = (await session.execute(select(SolutionCardRow))).scalars().one()

    assert row.requirement_checks == []
    assert row.unsourced_claims == []
    assert row.pdf_urls == []


async def test_new_columns_default_to_empty_list_on_bare_insert(db_session_factory):
    """Значение колонки по умолчанию — пустой список, как у соседних JSON-полей.
    Строка, вставленная без этих колонок, не должна давать NULL."""
    async with db_session_factory() as session:
        request_row = RequestRow(raw_text="строка без новых колонок", status="drafted", cost_rub=0.0)
        session.add(request_row)
        await session.flush()
        session.add(
            SolutionCardRow(
                request_id=request_row.id,
                title="Карточка без новых колонок",
                essence="суть",
                maturity_stage="unknown",
                applicability="применимость",
                next_step="следующий шаг",
            )
        )

    async with db_session_factory() as session:
        row = (await session.execute(select(SolutionCardRow))).scalars().one()

    assert row.requirement_checks == []
    assert row.unsourced_claims == []
    assert row.pdf_urls == []


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


# ── Т-7: номер записанного запроса доезжает до отчёта ───────────────────────
#
# До Т-7 `persist_run` не возвращала ничего, `Report.request_id` оставался
# нулём, и экспорт печатал «Запрос №—». Номер существовал только в БД: связать
# выданный клиенту отчёт со строкой журнала и с карточками было нечем.


async def test_persist_run_returns_id_of_written_request(db_session_factory):
    """`persist_run` возвращает id созданной строки `requests` — тот самый, что лежит в базе."""
    returned = await persist_run(
        db_session_factory,
        raw_text="текст задачи, у которой должен появиться номер",
        status="drafted",
        cost_rub=0.0,
        cards=[_card()],
        stages=[],
    )

    async with db_session_factory() as session:
        row = (await session.execute(select(RequestRow))).scalars().one()
        card = (await session.execute(select(SolutionCardRow))).scalars().one()

    assert returned == row.id
    assert returned > 0
    # Номер обязан быть настоящим ключом, а не порядковым номером в памяти:
    # по нему карточки прогона находятся в базе.
    assert card.request_id == returned


async def test_persist_run_returns_none_when_write_fails(caplog):
    """Отказ записи возвращает `None` и НЕ бросает: прогон не роняется (инвариант №5, РЕШ-43).

    Прочерк вместо номера — честный признак того, что записи не было; молчаливая
    подстановка любого числа выдала бы отказ БД за успешную запись.
    """

    def broken_factory():
        raise RuntimeError("база залокирована другим процессом")

    with caplog.at_level(logging.WARNING):
        returned = await persist_run(
            broken_factory,
            raw_text="текст задачи при сломанной фабрике",
            status="drafted",
            cost_rub=0.0,
            cards=[_card()],
            stages=[],
        )

    assert returned is None
    # Отказ не роняет прогон, но и не прячется: он обязан остаться в логе.
    assert any("запись прогона в БД не удалась" in record.message for record in caplog.records)


async def test_persist_run_returns_none_without_factory():
    """Фабрики нет — записи нет, номера нет. Не ноль и не исключение, а `None`."""
    assert (
        await persist_run(
            None,
            raw_text="текст задачи без фабрики сессий",
            status="drafted",
            cost_rub=0.0,
            cards=[_card()],
            stages=[],
        )
        is None
    )


async def test_run_puts_persisted_id_into_report(settings, brief, fake_provider_factory, db_session_factory):
    """Оркестратор ставит номер записанного запроса в `Report.request_id` до возврата отчёта."""
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_OneHitAdapter(client)], session_factory=db_session_factory)
        # request_id=0 — как из `scout run`: номера до записи не существует.
        report = await pipeline.run(brief, request_id=0, raw_text="текст задачи с номером в отчёте")

    async with db_session_factory() as session:
        row = (await session.execute(select(RequestRow))).scalars().one()

    assert report.request_id == row.id
    assert report.request_id > 0, "нулевой номер экспорт печатает прочерком — «Запрос №—»"


async def test_run_keeps_zero_request_id_when_write_fails(settings, brief, fake_provider_factory):
    """Отказ записи не даёт отчёту выдуманного номера: остаётся ноль, экспорт печатает прочерк."""
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])

    def broken_factory():
        raise RuntimeError("база залокирована другим процессом")

    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_OneHitAdapter(client)], session_factory=broken_factory)
        report = await pipeline.run(brief, request_id=0, raw_text="текст задачи без записи в БД")

    assert len(report.cards) == 1, "отказ записи не роняет прогон"
    assert report.request_id == 0
    assert "Запрос №—" in render_markdown(report)


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
