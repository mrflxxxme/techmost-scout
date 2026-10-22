"""Сквозной прогон слоя аналогов: от плана до отчёта и до базы (Э6-2а).

Что закрыто, по критериям приёмки задачи:

1. `research` — ни одного вызова `analog_scorer`, `analog_cards == []`, формулировки
   `analog` до источника не доходят (даже если их вернула модель), следов в журнале стадий нет.
2. `product` — до трёх аналогов вне `cards`, число основных карточек не тронуто, аналоги
   не попадают в общий отбор и во вход сводки, тип `analog` не засчитывается в покрытие.
3. Режим `gov`: кандидат `TW` не доходит до модели; `TW` от модели по кандидату без страны
   не публикуется.
4. Стоп-кран `CostLimitExceededError` из секции аналогов не перехватывается (РЕШ-45).

Сети нет: источник — поддельный адаптер, модель — провайдер-заглушка, отвечающий по
содержимому промпта, база — временный файл SQLite (инвариант №7).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.llm import CostLimitExceededError, LLMRouter
from app.llm.base import LLMResult, Message, Usage
from app.models import Base
from app.models import Brief as BriefRow
from app.models import Candidate as CandidateRow
from app.models import PipelineLog as PipelineLogRow
from app.models import Request as RequestRow
from app.pipeline import analogs, stages
from app.pipeline.orchestrator import ANALOG_CARDS_TARGET, ScoutPipeline
from app.schemas import (
    Brief,
    Candidate,
    Evidence,
    Report,
    ReportSummary,
    ScoredCandidate,
    SearchPlan,
    SearchQuery,
    SolutionCard,
    SourceType,
)
from app.search.base import SearchAdapter
from conftest import FakeProvider

MAIN_COUNT = 6
_ANALOG_URL = re.compile(r"https://analog\.example/\d+")
_COUNTRY = re.compile(r'"country": "([A-Z]{2})"')


def _main_candidate(number: int) -> Candidate:
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=f"Публикация №{number} о мониторинге температуры",
        url=f"https://doi.org/10.1234/main-{number}",
        org=f"Институт №{number}",
        year=2025,
        raw_id=f"10.1234/main-{number}",
        provider="crossref",
    )


def _analog_candidate(number: int, country: str | None) -> Candidate:
    return Candidate(
        source_type=SourceType.ANALOG,
        title=f"Cold chain monitoring service {number}",
        url=f"https://analog.example/{number}",
        org=f"Vendor {number}",
        year=2026,
        snippet="A cloud service that tracks cargo temperature in transit.",
        provider="yandex_search",
        country=country,
    )


class _Adapter(SearchAdapter):
    """Один источник на публикации и аналоги; помнит, какие формулировки ему задали."""

    name = "yandex_search"
    source_types = (SourceType.PUBLICATION, SourceType.ANALOG)

    def __init__(self, client: httpx.AsyncClient, analog_countries: list[str | None]) -> None:
        super().__init__(client)
        self._analog_countries = analog_countries
        self.queries: list[SearchQuery] = []
        self.returned: list[Candidate] = []

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        self.queries.append(query)
        if query.source_type is SourceType.ANALOG:
            found = [_analog_candidate(n, code) for n, code in enumerate(self._analog_countries, start=1)]
        else:
            found = [_main_candidate(n) for n in range(1, MAIN_COUNT + 1)]
        self.returned.extend(found)
        return found

    def analog_queries(self) -> list[SearchQuery]:
        return [q for q in self.queries if q.source_type is SourceType.ANALOG]


def _model_plan() -> str:
    """План «от модели»: с двумя `analog` — как будто модель не послушалась про `research`."""
    queries = [
        SearchQuery(text="мониторинг температуры", source_type=SourceType.PUBLICATION, lang="ru", rationale="тест"),
        SearchQuery(text="temperature monitoring", source_type=SourceType.PUBLICATION, lang="en", rationale="тест"),
        SearchQuery(text="cold chain monitoring saas", source_type=SourceType.ANALOG, lang="en", rationale="тест"),
        SearchQuery(text="pay per use cold chain", source_type=SourceType.ANALOG, lang="en", rationale="тест"),
    ]
    return SearchPlan(queries=queries).model_dump_json()


class _StageProvider(FakeProvider):
    """Заглушка, отвечающая по содержимому системного промпта.

    Планировщик получает `_model_plan()`, `analog_scorer` — карточку аналога со страной
    `analog_country` (по умолчанию — страной первого кандидата списка, как отвечает добросовестная
    модель) и ссылкой-основанием из СВОЕГО списка кандидатов; прочее — `{}`
    (судья дублей на `{}` честно отказывает, как в `test_contest_section_flow.py`).
    """

    def __init__(self, analog_country: str | None = None, *, fail_analog: Exception | None = None) -> None:
        super().__init__("deepseek")
        self.analog_country = analog_country
        self.fail_analog = fail_analog
        self.analog_prompts: list[str] = []

    async def complete(self, messages: list[Message], **kwargs: object) -> LLMResult:
        self.calls.append(messages)
        system = messages[0].content
        if "`AnalogCard`" in system:
            self.analog_prompts.append(system)
            if self.fail_analog is not None:
                raise self.fail_analog
            text = self._analog_answer(system)
        elif "`SearchPlan`" in system:
            text = _model_plan()
        else:
            text = "{}"
        return LLMResult(
            text=text, usage=Usage(tokens_in=100, tokens_out=50, cost_rub=0.01, provider=self.name, model="fake")
        )

    def _analog_answer(self, system: str) -> str:
        candidates_block = system.split("## Вход")[-1]
        urls = _ANALOG_URL.findall(candidates_block)
        country = self.analog_country or (_COUNTRY.findall(candidates_block) or ["US"])[0]
        return json.dumps(
            {
                "title": "Мониторинг холодовой цепи",
                "essence": "Облачный сервис следит за температурой груза.",
                "country": country,
                "country_evidence_url": urls[0],
                "business_model": "Подписка.",
                "traction": "",
                "transferability_ru": "не установлено",
                "what_to_do_in_ru": "Проверить поставки в РФ у разработчика.",
                "evidence": [{"claim": "Сервис следит за температурой", "url": urls[0]}],
            },
            ensure_ascii=False,
        )

    def called_with(self, fragment: str) -> int:
        """Сколько вызовов модели содержали фрагмент в системном промпте."""
        return sum(1 for messages in self.calls if fragment in messages[0].content)


@pytest.fixture
def stub_stages(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[list[SolutionCard]]]:
    """Стадии модели, кроме планировщика и аналогов, — заглушки. Возвращает вход сводки."""
    summary_inputs: list[list[SolutionCard]] = []

    async def fake_score(router, brief: Brief, candidates: list[Candidate], **kwargs) -> list[ScoredCandidate]:
        return [
            ScoredCandidate(candidate=candidate, relevance=6, maturity=5, accessibility=5, freshness=5, completeness=5)
            for candidate in candidates
        ]

    async def fake_author(router, brief: Brief, group: list[ScoredCandidate]) -> SolutionCard:
        candidate = group[0].candidate
        return SolutionCard(
            title=candidate.title,
            essence="Суть решения одной строкой.",
            applicability="Закрывает задачу.",
            next_step="Связаться с разработчиком.",
            evidence=[Evidence(claim="Подтверждение из источника", url=candidate.url)],
            candidate_urls=[candidate.url],
        )

    async def fake_verify(router, card: SolutionCard, group=None) -> SolutionCard:
        return card

    async def fake_summary(router, brief: Brief, cards: list[SolutionCard]) -> ReportSummary:
        summary_inputs.append(list(cards))
        return ReportSummary(recommendation="Начать с варианта №1.")

    monkeypatch.setattr(stages, "score_candidates", fake_score)
    monkeypatch.setattr(stages, "author_card", fake_author)
    monkeypatch.setattr(stages, "verify_card", fake_verify)
    monkeypatch.setattr(stages, "summarize", fake_summary)
    return {"summary_inputs": summary_inputs}  # type: ignore[dict-item]


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analog-test.db'}", future=True)
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


async def _run(
    settings,
    brief: Brief,
    *,
    kind: str,
    mode: str = "business",
    analog_countries: list[str | None] | None = None,
    provider: _StageProvider | None = None,
    session_factory=None,
    progress=None,
) -> tuple[Report, _Adapter, _StageProvider]:
    provider = provider or _StageProvider()
    router = LLMRouter(settings, providers={"deepseek": provider})
    run_brief = brief.model_copy(update={"solution_kind": kind, "mode": mode})
    async with httpx.AsyncClient() as client:
        adapter = _Adapter(client, analog_countries if analog_countries is not None else ["US", "DE", "US", "DE", "US"])
        pipeline = ScoutPipeline(
            settings=settings,
            router=router,
            adapters=[adapter],
            session_factory=session_factory,
            progress=progress,
        )
        report = await pipeline.run(run_brief, request_id=1, raw_text="текст задачи про мониторинг температуры")
    return report, adapter, provider


# ── Критерий 1: «Наука» не знает про аналоги ─────────────────────────────────
async def test_research_не_зовёт_analog_scorer_и_не_шлёт_analog_источнику(
    settings, brief, stub_stages, db_session_factory
):
    """План «от модели» содержит `analog`; при `research` код его снимает ДО источника."""
    events: list[str] = []

    async def progress(stage: str, message: str, fraction: float) -> None:
        events.append(message)

    report, adapter, provider = await _run(
        settings, brief, kind="research", session_factory=db_session_factory, progress=progress
    )

    assert adapter.analog_queries() == [], "формулировки analog дошли до источника при research"
    assert provider.called_with("`AnalogCard`") == 0, "analog_scorer вызван при research"
    assert report.analog_cards == []
    assert len(report.cards) == MAIN_COUNT
    assert not any("Аналог" in message for message in events), "«Наука» не должна печатать строки про аналоги"
    async with db_session_factory() as session:
        stages_written = {row.stage for row in (await session.execute(select(PipelineLogRow))).scalars().all()}
    assert "analog_cards" not in stages_written


async def test_research_даже_с_кандидатами_типа_analog_не_строит_секцию(settings, brief, stub_stages, monkeypatch):
    """Страховка второго уровня: кандидаты-аналоги всё же пришли (чужой адаптер) — модель всё равно не зовётся."""
    monkeypatch.setattr(stages, "plan_search", _fixed_plan_search)

    report, adapter, provider = await _run(settings, brief, kind="research")

    assert adapter.analog_queries(), "условие теста: аналогов запросили"
    assert provider.called_with("`AnalogCard`") == 0
    assert report.analog_cards == []


async def _fixed_plan_search(router, brief: Brief) -> SearchPlan:
    return SearchPlan.model_validate_json(_model_plan())


# ── Критерий 2: «Рынок» — до трёх аналогов вне основной выдачи ───────────────
async def test_product_даёт_до_трёх_аналогов_вне_cards(settings, brief, stub_stages, db_session_factory):
    report, adapter, provider = await _run(settings, brief, kind="product", session_factory=db_session_factory)

    assert len(adapter.analog_queries()) <= 3
    assert [q.lang for q in adapter.analog_queries()].count("en") == 2
    assert [q.lang for q in adapter.analog_queries()].count("ru") <= 1  # русскую добавляет код (Э6-8)
    assert len(report.analog_cards) == ANALOG_CARDS_TARGET
    assert provider.called_with("`AnalogCard`") == ANALOG_CARDS_TARGET
    # `target_cards` не задет: основная секция осталась шестью карточками без аналогов.
    assert len(report.cards) == MAIN_COUNT
    assert all("Cold chain" not in card.title for card in report.cards)
    for card in report.analog_cards:
        assert [str(url) for url in card.candidate_urls] != [], "ссылки группы ставит код"
        assert str(card.country_evidence_url) in {str(url) for url in card.candidate_urls}

    async with db_session_factory() as session:
        stages_written = [row.stage for row in (await session.execute(select(PipelineLogRow))).scalars().all()]
        saved = (await session.execute(select(RequestRow))).scalars().one()
    assert stages_written.count("analog_cards") == 1
    assert stages_written.index("analog_cards") > stages_written.index("summary"), "секция аналогов — последняя"
    assert len(json.loads(saved.report_json)["analog_cards"]) == ANALOG_CARDS_TARGET, "аналоги не доехали до базы"


async def test_аналоги_не_попадают_в_общий_отбор_и_в_сводку(settings, brief, stub_stages, monkeypatch):
    seen: list[list[ScoredCandidate]] = []
    original = stages.select_top

    def spy(scored: list[ScoredCandidate], **kwargs) -> list[ScoredCandidate]:
        seen.append(list(scored))
        return original(scored, **kwargs)

    monkeypatch.setattr(stages, "select_top", spy)

    report, _, _ = await _run(settings, brief, kind="digital")

    assert seen, "select_top не вызывался — тест сторожил бы пустоту"
    for call in seen:
        assert all(item.candidate.source_type is not SourceType.ANALOG for item in call)
    # Вход сводки — ровно основные карточки отчёта, без аналогов.
    (summary_input,) = stub_stages["summary_inputs"]
    assert [card.title for card in summary_input] == [card.title for card in report.cards]
    assert report.analog_cards, "условие теста: аналоги в отчёте есть"


async def test_аналоги_не_засчитываются_в_покрытие_типов(settings, brief, stub_stages):
    _, adapter, _ = await _run(settings, brief, kind="business_model")

    without_analogs = [item for item in adapter.returned if item.source_type is not SourceType.ANALOG]
    assert any(item.source_type is SourceType.ANALOG for item in adapter.returned)
    assert stages.count_source_type_coverage(adapter.returned) == stages.count_source_type_coverage(without_analogs)


async def test_судьба_кандидатов_аналогов_в_базе(settings, brief, stub_stages, db_session_factory):
    """Отобранный аналог получает `selected`, не попавший в секцию — `rejected`."""
    await _run(settings, brief, kind="product", session_factory=db_session_factory)

    async with db_session_factory() as session:
        rows = (await session.execute(select(CandidateRow))).scalars().all()
    analog_rows = [row for row in rows if row.source_type == SourceType.ANALOG.value]
    assert len(analog_rows) == 5
    assert sum(1 for row in analog_rows if row.status == "selected") == ANALOG_CARDS_TARGET
    assert sum(1 for row in analog_rows if row.status == "rejected") == 5 - ANALOG_CARDS_TARGET


async def test_строка_прогресса_аналогов_печатается_и_при_нулях(settings, brief, stub_stages):
    events: list[str] = []

    async def progress(stage: str, message: str, fraction: float) -> None:
        events.append(message)

    report, _, provider = await _run(settings, brief, kind="product", analog_countries=[], progress=progress)

    assert report.analog_cards == []
    assert provider.called_with("`AnalogCard`") == 0
    assert "Аналоги из мира: кандидатов 0, заготовок карточек 0 (секция вне лимита 5–7)" in events
    assert "Аналогов из мира: 0, отброшено: 0" in events


# ── Критерий 3: страна и режим «Гос» ─────────────────────────────────────────
async def test_кандидат_tw_в_gov_не_доходит_до_модели(settings, brief, stub_stages):
    report, _, provider = await _run(settings, brief, kind="product", mode="gov", analog_countries=["TW", "US", "TW"])

    assert provider.called_with("`AnalogCard`") == 1, "модель платила только за допустимую страну"
    for prompt in provider.analog_prompts:
        assert "https://analog.example/1" not in prompt, "кандидат TW ушёл в промпт"
        assert "https://analog.example/3" not in prompt, "кандидат TW ушёл в промпт"
    assert [card.country for card in report.analog_cards] == ["US"]


async def test_tw_в_business_допустим_и_доходит_до_модели(settings, brief, stub_stages):
    """Парная проверка: фильтр зависит от режима, а не снимает страну везде."""
    report, _, provider = await _run(settings, brief, kind="product", mode="business", analog_countries=["TW"])

    assert provider.called_with("`AnalogCard`") == 1
    assert [card.country for card in report.analog_cards] == ["TW"]


async def test_tw_от_модели_по_кандидату_без_страны_в_gov_не_публикуется(settings, brief, stub_stages):
    provider = _StageProvider(analog_country="TW")

    report, _, _ = await _run(settings, brief, kind="product", mode="gov", analog_countries=[None], provider=provider)

    assert provider.called_with("`AnalogCard`") == 1, "условие теста: модель карточку собрала"
    assert report.analog_cards == []


async def test_та_же_карточка_tw_в_business_публикуется(settings, brief, stub_stages):
    """Парная проверка к предыдущему: без режима «Гос» карточка проходит, значит отсев именно по стране."""
    provider = _StageProvider(analog_country="TW")

    report, _, _ = await _run(
        settings, brief, kind="product", mode="business", analog_countries=[None], provider=provider
    )

    assert [card.country for card in report.analog_cards] == ["TW"]


async def test_страна_модели_против_структурной_не_публикуется(settings, brief, stub_stages):
    provider = _StageProvider(analog_country="US")

    report, _, _ = await _run(settings, brief, kind="product", analog_countries=["DE"], provider=provider)

    assert provider.called_with("`AnalogCard`") == 1
    assert report.analog_cards == []


# ── Критерий 4: отказы и стоп-кран ───────────────────────────────────────────
async def test_стоп_кран_из_секции_аналогов_не_перехватывается(settings, brief, stub_stages):
    provider = _StageProvider(fail_analog=CostLimitExceededError("лимит исчерпан"))

    with pytest.raises(CostLimitExceededError):
        await _run(settings, brief, kind="product", provider=provider)


async def test_отказ_модели_по_аналогу_не_роняет_прогон(settings, brief, stub_stages, monkeypatch):
    async def failing(router, brief_: Brief, group: list[ScoredCandidate]):
        raise ValueError("ответ модели не разобран")

    monkeypatch.setattr(analogs, "author_analog_card", failing)

    report, _, _ = await _run(settings, brief, kind="product")

    assert report.analog_cards == []
    assert len(report.cards) == MAIN_COUNT


# ── Совет ревью Э6-1: вид решения пишется в briefs на insert ─────────────────
async def test_вид_решения_пишется_в_колонку_briefs(settings, brief, stub_stages, db_session_factory):
    await _run(settings, brief, kind="digital", session_factory=db_session_factory)

    async with db_session_factory() as session:
        row = (await session.execute(select(BriefRow))).scalars().one()
    assert row.solution_kind == "digital"
