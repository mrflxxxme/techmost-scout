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
потерь и что карточка без них даёт пустые списки, а не NULL. Тем же тестом
закрыт статус прав `ip_status` (Т-22, РЕШ-79 п. 1): поле уходит клиенту в
отчёте, а его запись до Т-25 не проверялась ничем — удаление строки записи
проходило весь набор молча.

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
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage
from app.metrics import max_relevance, share_at_least_relevance
from app.models import Base, PipelineLog
from app.models import Candidate as CandidateRow
from app.models import Request as RequestRow
from app.models import SearchPlan as SearchPlanRow
from app.models import SolutionCard as SolutionCardRow
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.pipeline.persistence import PlanRecord, SearchPlanNotFoundError, load_search_plan, persist_run
from app.schemas import (
    Brief,
    Candidate,
    Confidence,
    Evidence,
    IPStatus,
    MaturityStage,
    RequirementCheck,
    SearchPlan,
    SearchQuery,
    SolutionCard,
    SourceType,
)
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


def _pipeline(
    settings,
    provider: LLMProvider,
    adapters: list[SearchAdapter],
    *,
    session_factory=None,
    preset_plan: SearchPlan | None = None,
) -> ScoutPipeline:
    router = LLMRouter(settings, providers={"deepseek": provider})
    return ScoutPipeline(
        settings=settings,
        router=router,
        adapters=adapters,
        session_factory=session_factory,
        preset_plan=preset_plan,
    )


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
        ip_status=IPStatus.TEAM,
    )

    await persist_run(
        db_session_factory,
        raw_text="текст задачи с новыми полями карточки",
        status="drafted",
        cost_rub=0.0,
        cards=[card],
        contest_cards=[],
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
    # Статус прав (Т-22, РЕШ-79 п. 1) печатается клиенту и обязан доехать до базы
    # ровно тем, что стоит в карточке.
    assert row.ip_status == IPStatus.TEAM.value
    # Значение проверки нарочно отличается от умолчания колонки: совпади они —
    # пропавшая строка записи выглядела бы успешной, и тест ничего не ловил бы.
    assert SolutionCardRow.__table__.c.ip_status.default.arg == IPStatus.UNKNOWN.value


async def test_card_without_new_fields_persists_empty_lists_not_null(db_session_factory):
    """Карточка без новых полей даёт пустые списки, а не NULL: иначе чтение
    обратно пришлось бы всюду страховать проверкой на None."""
    await persist_run(
        db_session_factory,
        raw_text="текст задачи без новых полей карточки",
        status="drafted",
        cost_rub=0.0,
        cards=[_card()],
        contest_cards=[],
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
        contest_cards=[],
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
            contest_cards=[],
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
            contest_cards=[],
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


# ── Т-19 (РЕШ-83): план поиска в БД и прогон по сохранённому плану ───────────
#
# Пока план строится моделью заново каждый раз, эффект правок поиска
# неотличим от разброса между прогонами: один и тот же запрос агропрома дал
# 92 и 72 кандидата в двух прогонах Э2-А. Здесь проверяется, что план
# доезжает до `search_plans` без потерь и что прогон по сохранённому плану не
# зовёт планировщик вовсе.


class _QueryRecordingAdapter(SearchAdapter):
    """Источник, запоминающий текст каждой формулировки, с которой его позвали.

    Нужен ровно для одного утверждения: формулировки прогона по сохранённому
    плану — это формулировки из базы, а не заново придуманные моделью.
    """

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    def __init__(self, client) -> None:
        super().__init__(client)
        self.seen: list[str] = []

    async def search(self, query, *, limit: int = 20) -> list[Candidate]:
        self.seen.append(query.text)
        return [
            Candidate(
                source_type=SourceType.PUBLICATION,
                title="Катализатор с присадками для сероустойчивости",
                url="https://doi.org/10.1/test-preset-plan",
                org="Институт катализа",
                year=2025,
                snippet="Испытан на действующей установке гидроочистки.",
                raw_id="10.1/test-preset-plan",
                provider="crossref",
            )
        ]


def _plan(text: str = "катализатор гидроочистки серостойкий") -> SearchPlan:
    """Минимальный план из одной формулировки под публикации."""
    return SearchPlan(
        queries=[
            SearchQuery(
                text=text,
                source_type=SourceType.PUBLICATION,
                lang="ru",
                rationale="прямой поиск по задаче",
            )
        ]
    )


async def test_search_plan_round_trips_through_db_without_loss(db_session_factory):
    """Круговой перегон `SearchPlan → БД → SearchPlan`: `model_dump()` совпадает.

    Сравнение целиком, а не по отдельным полям: план сохраняется дампом схемы,
    и молча потерянное поле (`rationale`, `lang`) всплыло бы только на платном
    прогоне по сохранённому плану.
    """
    plan = _plan()

    await persist_run(
        db_session_factory,
        raw_text="текст задачи, у которой план поиска обязан сохраниться",
        status="drafted",
        cost_rub=0.0,
        cards=[_card()],
        contest_cards=[],
        stages=[],
        plans=[PlanRecord(plan=plan)],
    )

    async with db_session_factory() as session:
        row = (await session.execute(select(SearchPlanRow))).scalars().one()
        request_row = (await session.execute(select(RequestRow))).scalars().one()

    assert row.request_id == request_row.id, "план обязан быть привязан к своему прогону"
    assert row.pass_no == 1
    assert row.created_at is not None
    assert SearchPlan.model_validate(row.queries).model_dump() == plan.model_dump()


async def test_successful_run_persists_exactly_one_plan_row(settings, brief, fake_provider_factory, db_session_factory):
    """Обычный прогон кладёт в `search_plans` ровно одну строку — план, по
    которому опрашивались источники, с `pass_no = 1`."""
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_OneHitAdapter(client)], session_factory=db_session_factory)
        await pipeline.run(brief, request_id=1, raw_text="текст задачи с сохранением плана")

    async with db_session_factory() as session:
        rows = (await session.execute(select(SearchPlanRow))).scalars().all()

    assert len(rows) == 1, "один проход — одна строка плана"
    assert rows[0].pass_no == 1
    saved = SearchPlan.model_validate(rows[0].queries)
    # З-3 (РЕШ-121): plan_search гарантирует ровно две конкурсные формулировки
    # кодом, а `_PLAN_JSON` их не содержит — normalize_contest_queries
    # достраивает обе из `brief.keywords_ru` до записи плана в search_plans.
    assert [query.text for query in saved.queries] == [
        "катализатор гидроочистки серостойкий",
        "хакатон конкурс решение команды катализатор гидроочистки",
        "итоги хакатона победитель проект команды катализатор гидроочистки",
    ]


async def test_two_passes_persist_two_rows_with_their_own_pass_no(db_session_factory):
    """Два прохода одного прогона дают две строки со своими номерами.

    Замок для Т-21 (второй проход, РЕШ-81) ставится здесь, пока схема ещё
    заводится: `pass_no`, всегда записанный единицей, роняет этот тест — иначе
    второй проход было бы нечем отличить от первого, а `create_all` не
    добавляет колонки в уже существующую таблицу.
    """
    first = _plan("катализатор гидроочистки серостойкий")
    second = _plan("присадка против отравления катализатора серой")

    await persist_run(
        db_session_factory,
        raw_text="текст задачи с двумя проходами поиска",
        status="drafted",
        cost_rub=0.0,
        cards=[_card()],
        contest_cards=[],
        stages=[],
        plans=[PlanRecord(plan=first, pass_no=1), PlanRecord(plan=second, pass_no=2)],
    )

    async with db_session_factory() as session:
        rows = (await session.execute(select(SearchPlanRow).order_by(SearchPlanRow.pass_no))).scalars().all()

    assert [row.pass_no for row in rows] == [1, 2]
    assert SearchPlan.model_validate(rows[0].queries).model_dump() == first.model_dump()
    assert SearchPlan.model_validate(rows[1].queries).model_dump() == second.model_dump()


async def test_run_with_preset_plan_does_not_call_planner_and_logs_zero_cost(
    settings, brief, fake_provider_factory, db_session_factory
):
    """Прогон по сохранённому плану: стадия `planner` к модели не обращается.

    Проверяется тремя способами сразу, потому что каждый по отдельности
    мутанта пропускает: (1) провайдеру отдан набор ответов БЕЗ плана, и лишний
    вызов увёл бы разбор оценок в сборку карточки; (2) в источник ушла ровно
    та формулировка, что лежала в плане; (3) строка `planner` журнала стадий
    стоит с нулевой стоимостью — иначе «план из базы» списывал бы деньги.
    """
    plan = _plan("формулировка из сохранённого плана")
    provider = fake_provider_factory("deepseek", [_SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        adapter = _QueryRecordingAdapter(client)
        pipeline = _pipeline(settings, provider, [adapter], session_factory=db_session_factory, preset_plan=plan)
        report = await pipeline.run(brief, request_id=1, raw_text="текст задачи по сохранённому плану")

    assert len(report.cards) == 1
    assert len(provider.calls) == 4, "вызовов модели ровно четыре: скоринг, карточка, проверка, сводка"
    assert adapter.seen == ["формулировка из сохранённого плана"]

    async with db_session_factory() as session:
        found = await session.execute(select(PipelineLog).where(PipelineLog.stage == "planner"))
        planner_logs = found.scalars().all()
        plan_rows = (await session.execute(select(SearchPlanRow))).scalars().all()

    assert len(planner_logs) == 1
    assert planner_logs[0].cost_rub == 0.0
    assert planner_logs[0].provider is None
    # Прогон по чужому плану — тоже прогон: его формулировки обязаны
    # сохраниться, иначе восстановить их потом неоткуда.
    assert len(plan_rows) == 1
    assert SearchPlan.model_validate(plan_rows[0].queries).model_dump() == plan.model_dump()


async def test_normalize_contest_queries_план_на_потолке_переживает_search_plans_и_load_search_plan(
    db_session_factory,
):
    """РЕШ-127 п. 1, круговой перегон: план из 12 формулировок без единой
    `contest` (потолок `SearchPlan.queries`) после `normalize_contest_queries`
    обязан не только пройти `model_validate` на месте (это отдельный тест в
    `tests/test_contest_queries.py`), но и пережить настоящую запись в
    `search_plans` и обратное чтение через `load_search_plan` — до правки
    план из 14 формулировок писался в базу молча, а `load_search_plan` читал
    его через ту же схему `SearchPlan` и падал `SearchPlanNotFoundError` на
    прогоне `--plan-id`, хотя запись «успешно» состоялась."""
    brief = Brief(industry="фарма", task_summary="Диагностика супоросности свиноматок по УЗИ.")
    raw_plan = SearchPlan(
        queries=[
            SearchQuery(text=f"формулировка основной очереди {i}", source_type=SourceType.PUBLICATION, rationale="тест")
            for i in range(12)
        ]
    )
    normalized = stages.normalize_contest_queries(raw_plan, brief)
    assert len(normalized.queries) == 12, "нормализация обязана соблюдать потолок схемы уже на этом шаге"

    persisted_id = await persist_run(
        db_session_factory,
        raw_text="текст задачи с планом на потолке схемы",
        status="drafted",
        cost_rub=0.0,
        cards=[],
        contest_cards=[],
        stages=[],
        plans=[PlanRecord(plan=normalized)],
    )

    async with db_session_factory() as session:
        row = (await session.execute(select(SearchPlanRow))).scalars().one()
    assert row.request_id == persisted_id

    loaded = await load_search_plan(db_session_factory, row.id)  # не должно поднимать SearchPlanNotFoundError
    assert loaded.model_dump() == normalized.model_dump()


async def test_load_search_plan_returns_saved_plan(db_session_factory):
    """`load_search_plan` отдаёт ровно тот план, который записали."""
    plan = _plan()
    await persist_run(
        db_session_factory,
        raw_text="текст задачи для чтения плана обратно",
        status="drafted",
        cost_rub=0.0,
        cards=[],
        contest_cards=[],
        stages=[],
        plans=[PlanRecord(plan=plan)],
    )

    async with db_session_factory() as session:
        row = (await session.execute(select(SearchPlanRow))).scalars().one()

    assert (await load_search_plan(db_session_factory, row.id)).model_dump() == plan.model_dump()


async def test_load_search_plan_raises_on_unknown_id(db_session_factory):
    """Несуществующий номер — понятный отказ с номером в тексте, а не None."""
    with pytest.raises(SearchPlanNotFoundError) as info:
        await load_search_plan(db_session_factory, 4242)

    assert "4242" in str(info.value)
    assert "search_plans" in str(info.value)


async def test_load_search_plan_raises_on_unreadable_row(db_session_factory):
    """Битая строка отказывает до первого платного запроса, а не даёт пустой план.

    В базе может лежать что угодно (правка руками, план от несовместимой
    версии схемы). Пустой `SearchPlan` из такой строки означал бы прогон без
    единой формулировки — половина прогона впустую и оплаченный интейк.
    """
    async with db_session_factory() as session:
        request_row = RequestRow(raw_text="прогон с испорченным планом", status="drafted", cost_rub=0.0)
        session.add(request_row)
        await session.flush()
        session.add(SearchPlanRow(request_id=request_row.id, pass_no=1, queries={"queries": []}))

    async with db_session_factory() as session:
        row = (await session.execute(select(SearchPlanRow))).scalars().one()

    with pytest.raises(SearchPlanNotFoundError) as info:
        await load_search_plan(db_session_factory, row.id)

    assert "SearchPlan" in str(info.value)


async def test_load_search_plan_raises_without_factory():
    """Без фабрики сессий читать план неоткуда — отказ, а не молчаливый None."""
    with pytest.raises(SearchPlanNotFoundError):
        await load_search_plan(None, 1)


# ── Т-20 (РЕШ-84): кандидаты с оценками в БД ────────────────────────────────
#
# До Т-20 таблица `candidates` была объявлена и не писалась ни одной строкой
# кода: оценки скоринга жили в единственной строке журнала калибровки и умирали
# вместе с прогоном. Числовой критерий приёмки подэтапа (РЕШ-86 — «максимум
# `relevance` вырос с 4 до ≥6») подтверждать было нечем.


class _ThreeHitsAdapter(SearchAdapter):
    """Источник с тремя записями, две из которых — один и тот же DOI.

    Дубль здесь не для проверки склейки: он нужен, чтобы «строк столько же,
    сколько кандидатов» было отличимо от «строк столько же, сколько найдено».

    Организация у всех одна: `group_for_cards` группирует по точной строке
    `org`, и разные организации давали бы две карточки, собираемые параллельно,
    — тогда порядок ответов провайдера-заглушки перестал бы быть определённым.
    """

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query, *, limit: int = 20) -> list[Candidate]:
        common = {
            "source_type": SourceType.PUBLICATION,
            "provider": "crossref",
            "year": 2025,
            "org": "Институт катализа",
        }
        return [
            Candidate(
                title="Катализатор с присадками для сероустойчивости",
                url="https://doi.org/10.1/test-candidates-a",
                snippet="Испытан на действующей установке гидроочистки.",
                raw_id="10.1/test-candidates-a",
                **common,
            ),
            Candidate(
                title="Тот же результат в сборнике конференции",
                url="https://cyberleninka.ru/article/n/test-candidates-a",
                snippet="Тот же DOI — обязан склеиться в одного кандидата.",
                raw_id="10.1/test-candidates-a",
                **common,
            ),
            Candidate(
                title="Обзор методов регенерации катализаторов",
                url="https://doi.org/10.1/test-candidates-b",
                snippet="Обзорная работа без промышленной апробации.",
                raw_id="10.1/test-candidates-b",
                **common,
            ),
        ]


class _ScorerFailsProvider(LLMProvider):
    """Провайдер, отказывающий ровно на батче скоринга.

    Так воспроизводится единственный путь появления нейтральных оценок:
    `stages.score_candidates` ловит отказ батча и ставит пятёрки по всем шкалам
    кодом. Отказ непереспрашиваемый — иначе роутер потратил бы прогон на
    повторы, а тест — секунды на паузы между ними.
    """

    name = "deepseek"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
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
        # Батч скоринга узнаётся по требованию вернуть оценки номерами — эту
        # фразу собирает `stages.score_candidates` и никакая другая стадия.
        if any("Верни ровно" in message.content for message in messages):
            raise LLMError(self.name, "тестовый отказ батча скоринга", retryable=False)
        text = self._responses.pop(0) if self._responses else "{}"
        return LLMResult(
            text=text,
            usage=Usage(tokens_in=100, tokens_out=50, cost_rub=0.01, provider=self.name, model=model or "fake"),
        )

    async def health(self) -> tuple[bool, str]:
        return True, "заглушка"


_SCORE_TWO_JSON = json.dumps(
    {
        "scores": [
            {"index": 0, "relevance": 8, "maturity": 6, "accessibility": 7, "freshness": 6, "completeness": 7},
            {"index": 1, "relevance": 2, "maturity": 5, "accessibility": 5, "freshness": 5, "completeness": 5},
        ]
    }
)


async def test_run_persists_one_candidate_row_per_deduplicated_candidate(
    settings, brief, fake_provider_factory, db_session_factory, monkeypatch
):
    """Критерий 1 Т-20: строк в `candidates` столько же, сколько кандидатов после
    склейки дублей, и у каждой заполнены `score` и `status`.

    Порог отбора поднят до рабочего значения (`RELEVANCE_MIN = 4`, РЕШ-74),
    чтобы в выдаче оказались обе судьбы сразу: `status`, записанный всем как
    `selected`, роняет этот тест. Без разделения таблица превращается в список
    найденного и на вопрос «что отбор реально взял» не отвечает.
    """
    monkeypatch.setattr(stages, "get_settings", lambda: settings.model_copy(update={"relevance_min": 4}))
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_TWO_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_ThreeHitsAdapter(client)], session_factory=db_session_factory)
        await pipeline.run(brief, request_id=1, raw_text="текст задачи с сохранением оценок")

    async with db_session_factory() as session:
        rows = (await session.execute(select(CandidateRow).order_by(CandidateRow.dedup_key))).scalars().all()
        request_row = (await session.execute(select(RequestRow))).scalars().one()

    assert len(rows) == 2, "строк должно быть столько же, сколько кандидатов после дедупликации, а не сколько найдено"
    assert all(row.request_id == request_row.id for row in rows)
    assert [row.dedup_key for row in rows] == ["10.1/test-candidates-a", "10.1/test-candidates-b"]
    assert all(row.pass_no == 1 for row in rows), "до Т-21 весь поиск — первый проход"
    assert all(row.source_type == "publication" and row.provider == "crossref" for row in rows)
    assert all(row.url for row in rows)

    selected_row, rejected_row = rows
    assert selected_row.status == "selected"
    assert rejected_row.status == "rejected", "все кандидаты записаны отобранными — судьба отбора потеряна"
    assert selected_row.score == {
        "relevance": 8,
        "maturity": 6,
        "accessibility": 7,
        "freshness": 6,
        "completeness": 7,
        "total": pytest.approx(7.05),
        "notes": "",
        "neutral": False,
    }
    assert rejected_row.score["relevance"] == 2


async def test_failed_scoring_batch_is_marked_neutral_in_saved_scores(
    settings, brief, db_session_factory, monkeypatch, caplog
):
    """Оценки, проставленные кодом после отказа батча, отличимы в базе.

    Без признака `neutral` нейтральная пятёрка неотличима от честной пятёрки
    модели по самому числу — и прогон с полностью отказавшим скорингом показал
    бы измерителям выдачу «сплошь выше порога». Здесь это проверяется тем же
    путём, каким числа будут читаться на приёмке: измерителями по строкам БД.
    """
    provider = _ScorerFailsProvider([_PLAN_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    with caplog.at_level(logging.WARNING):
        async with httpx.AsyncClient() as client:
            pipeline = _pipeline(settings, provider, [_ThreeHitsAdapter(client)], session_factory=db_session_factory)
            await pipeline.run(brief, request_id=1, raw_text="текст задачи с отказавшим скорингом")

    assert any("скоринг батча не удался" in record.message for record in caplog.records)

    async with db_session_factory() as session:
        rows = (await session.execute(select(CandidateRow))).scalars().all()

    assert len(rows) == 2
    assert all(row.score["neutral"] is True for row in rows), "след отказа батча неотличим от оценок модели"
    assert all(row.score["relevance"] == 5 for row in rows)

    saved = [row.score for row in rows]
    assert max_relevance(saved) is None, "нейтральные пятёрки выданы за максимум выдачи"
    assert share_at_least_relevance(saved) is None


async def test_candidate_rows_survive_run_without_session_factory(settings, brief, fake_provider_factory, tmp_path):
    """Без фабрики сессий прогон идёт как раньше: оценки собираются, но не пишутся.

    Инвариант №5 в редакции РЕШ-43: запись вторична по отношению к отчёту.
    """
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_TWO_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_ThreeHitsAdapter(client)])
        report = await pipeline.run(brief, request_id=1, raw_text="текст задачи без записи оценок")

    assert len(report.cards) >= 1
    assert not (tmp_path / "persist-test.db").exists()


async def test_failed_run_keeps_scores_collected_before_the_failure(
    settings, brief, fake_provider_factory, db_session_factory
):
    """Прогон, упавший после скоринга, всё равно оставляет оценки в базе.

    Оценки — самое дорогое, что есть в прогоне: они оплачены и бесплатно не
    воспроизводятся. Терять их из-за стоп-крана на сборке карточек значило бы
    платить за скоринг дважды. Лимит подобран как в тестах РЕШ-45 выше: план и
    скоринг (0,02 ₽) укладываются в 0,025 ₽, сборка карточки — нет.
    """
    low_limit = settings.model_copy(update={"cost_limit_rub_per_request": 0.025})
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_TWO_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(low_limit, provider, [_ThreeHitsAdapter(client)], session_factory=db_session_factory)
        with pytest.raises(CostLimitExceededError):
            await pipeline.run(brief, request_id=1, raw_text="текст задачи, упавший после скоринга")

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        rows = (await session.execute(select(CandidateRow))).scalars().all()

    assert requests[0].status == "failed"
    assert len(rows) == 2, "оплаченные оценки потеряны вместе с упавшим прогоном"


async def test_load_search_plan_raises_when_database_is_broken():
    """Отказ самой базы тоже становится понятным отказом чтения плана.

    Читать план — не то же самое, что писать прогон: запись гасится и уходит в
    лог (инвариант №5 в редакции РЕШ-43), потому что отчёт первичен, а вот
    прогон по непрочитанному плану продолжать нечем.
    """

    def broken_factory():
        raise RuntimeError("база залокирована другим процессом")

    with pytest.raises(SearchPlanNotFoundError) as info:
        await load_search_plan(broken_factory, 1)

    assert "залокирована" in str(info.value)
