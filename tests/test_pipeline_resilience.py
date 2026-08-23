"""Устойчивость прогона и связка контракта проверки с кодом (Ф-7, РЕШ-42).

Проверяем: отказ стадии summary (сразу и после переспроса с увеличенным
лимитом токенов) не роняет прогон — отчёт собирается с уже готовыми
карточками и запасной сводкой без вызова модели, факт отказа виден в
caveats; прогресс сообщает фактическое число опубликованных и отброшенных
карточек; verify_card кладёт находки проверки в SolutionCard.unsourced_claims
и понижает maturity_stage до unknown, когда не подтверждена именно зрелость;
requirement_checks из ответа card_author доезжает до карточки и до
отрендеренного отчёта.

Провайдер-заглушка отвечает по ключевой фразе роли в системном промпте, а не
по номеру вызова: карточки нескольких групп собираются конкурентно
(`asyncio.gather` в `_author_cards`), и порядок вызовов между группами не
гарантирован. Сеть не задействована нигде — respx не нужен, поисковые
адаптеры подменены синхронными заглушками, как в tests/test_pipeline_counters.py.

Отдельно (Т-2, устойчивость интейка): отказ `refine_brief` в `cli._run` не
теряет ответы владельца — они ложатся в `Brief.owner_answers` (РЕШ-58) и
доходят оттуда до модели, а `constraints` остаются ограничениями задачи; ответ
«не знаю» записывается как «требование не специализировано»; отказавшая
стадия интейка, переданная оркестратору новым параметром `initial_stage_log`,
доезжает до `pipeline_logs` строкой с непустым `error`, а статус запроса
остаётся `drafted` — прогон дошёл до отчёта; `CostLimitExceededError` —
стоп-кран (РЕШ-45), `cli._run` обязан её пробросить, а не подменить на запасной
бриф, причём в обоих местах вызова: и на `run_intake`, и на `refine_brief`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.cli as cli
from app.export.markdown import render_markdown
from app.llm import CostLimitExceededError, LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage
from app.models import Base, PipelineLog
from app.models import Request as RequestRow
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.pipeline.persistence import StageRecord
from app.schemas import (
    Brief,
    Candidate,
    Confidence,
    Evidence,
    IntakeQuestion,
    IntakeResult,
    MaturityStage,
    OwnerAnswer,
    Report,
    ReportSummary,
    ScoredCandidate,
    SolutionCard,
    SourceType,
)
from app.search.base import SearchAdapter

# Ключевые фразы из "## Роль" каждого промпта — уникальны и достаточны, чтобы
# определить стадию по системному сообщению, не завися от порядка вызовов.
_PLANNER_KEY = "специалист по научно-техническому поиску"
_SCORER_KEY = "аналитик технологического скаутинга"
_CARD_AUTHOR_KEY = "технический писатель службы подбора"
_VERIFIER_KEY = "научный редактор-контролёр"
_SUMMARY_KEY = "руководитель проекта подбора"


class _ScriptedProvider(LLMProvider):
    """Провайдер-заглушка: ответ выбирается по ключевой фразе в системном
    сообщении, ответы на одну и ту же фразу выдаются по очереди, последний —
    повторно. Пустая очередь на фразу — отказ (для теста отказа summary)."""

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


class _OneHitAdapter(SearchAdapter):
    """Один рабочий источник, одна публикация на любую формулировку."""

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query, *, limit: int = 20) -> list[Candidate]:
        return [
            Candidate(
                source_type=SourceType.PUBLICATION,
                title="Катализатор с присадками для сероустойчивости",
                url="https://doi.org/10.1/example",
                org="Институт катализа",
                year=2025,
                snippet="Испытан на действующей установке гидроочистки.",
                raw_id="10.1/example",
                provider="crossref",
            )
        ]


class _TwoHitAdapter(SearchAdapter):
    """Два разных кандидата — по одному на каждую из двух формулировок плана,
    чтобы группировка дала две отдельные группы (разные организации)."""

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query, *, limit: int = 20) -> list[Candidate]:
        suffix = "a" if "первый" in query.text else "b"
        return [
            Candidate(
                source_type=SourceType.PUBLICATION,
                title=f"Кандидат {suffix}",
                url=f"https://doi.org/10.1/{suffix}",
                org=f"Организация {suffix}",
                year=2025,
                snippet="Фрагмент описания.",
                raw_id=f"10.1/{suffix}",
                provider="crossref",
            )
        ]


def _pipeline(
    settings, provider: LLMProvider, adapters: list[SearchAdapter], *, progress=None, session_factory=None
) -> ScoutPipeline:
    router = LLMRouter(settings, providers={"deepseek": provider})
    return ScoutPipeline(
        settings=settings, router=router, adapters=adapters, progress=progress, session_factory=session_factory
    )


@pytest.fixture
async def db_session_factory(tmp_path):
    """Фабрика сессий на временном файле SQLite — та же форма, что у
    `app.db.session_scope` (как в tests/test_persistence.py, без общего
    движка приложения: временная база через `tmp_path`, а не боевой
    `data/scout.db`)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resilience-test.db'}", future=True)
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

_PLAN_JSON_TWO = json.dumps(
    {
        "queries": [
            {
                "text": "первый вариант запроса по катализатору",
                "source_type": "publication",
                "lang": "ru",
                "rationale": "первая формулировка",
            },
            {
                "text": "второй вариант запроса по катализатору",
                "source_type": "publication",
                "lang": "ru",
                "rationale": "вторая формулировка",
            },
        ]
    },
    ensure_ascii=False,
)

_SCORE_JSON = json.dumps(
    {"scores": [{"index": 0, "relevance": 8, "maturity": 6, "accessibility": 7, "freshness": 6, "completeness": 7}]}
)

_SCORE_JSON_TWO = json.dumps(
    {
        "scores": [
            {"index": 0, "relevance": 8, "maturity": 6, "accessibility": 7, "freshness": 6, "completeness": 7},
            {"index": 1, "relevance": 7, "maturity": 5, "accessibility": 6, "freshness": 6, "completeness": 6},
        ]
    }
)


def _card_json(**overrides) -> str:
    payload = {
        "title": "Катализатор с присадками для сероустойчивости",
        "essence": "Катализатор гидроочистки с присадкой, продлевающей срок службы на сырье с серой.",
        "maturity_stage": "pilot",
        "trl_estimate": None,
        "developer_org": "Институт катализа",
        "developer_team": None,
        "contact_leads": [],
        "evidence": [{"claim": "Испытан на действующей установке гидроочистки", "url": "https://doi.org/10.1/example"}],
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "risks": [],
        "gov_support": [],
        "next_step": "Запросить у Института катализа протокол испытаний.",
        "confidence": "medium",
        "confidence_note": "Один источник",
        "candidate_urls": ["https://doi.org/10.1/example"],
        "requirement_checks": [],
        "unsourced_claims": [],
        "pdf_urls": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


_CARD_JSON = _card_json()
_PLACEHOLDER_CARD_JSON = json.dumps(
    {
        "title": "не найдено",
        "essence": "не найдено",
        "maturity_stage": "unknown",
        "trl_estimate": None,
        "developer_org": None,
        "developer_team": None,
        "contact_leads": [],
        "evidence": [],
        "applicability": "не найдено",
        "risks": ["Источники по этой группе отсутствуют: карточку собрать не из чего"],
        "gov_support": [],
        "next_step": "не найдено",
        "confidence": "low",
        "confidence_note": "В группе нет источников с рабочими ссылками",
        "candidate_urls": [],
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

#: Ответ, обрывающийся на середине — незакрытые скобки триггерят переспрос с
#: увеличенным лимитом токенов в LLMRouter.complete_json (_looks_truncated).
_TRUNCATED_SUMMARY = '{"comparison_rows": ['


# ── Отказ стадии summary не роняет прогон (критерий 1) ──────────────────────
async def test_summary_llm_error_does_not_crash_run(settings, brief):
    """summarize бросает LLMError сразу → отчёт с карточками и запасной
    сводкой, факт отказа в caveats, ни одного лишнего вызова модели."""
    provider = _ScriptedProvider(
        {
            _PLANNER_KEY: [_PLAN_JSON],
            _SCORER_KEY: [_SCORE_JSON],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [],  # заготовленных ответов нет — отказ на первой же попытке
        }
    )
    async with httpx.AsyncClient() as client:
        report = await _pipeline(settings, provider, [_OneHitAdapter(client)]).run(brief, request_id=1)

    assert len(report.cards) == 1
    assert report.summary.recommendation
    assert any("сводка" in caveat.lower() for caveat in report.summary.caveats)
    # план, скоринг, карточка, проверка — 4 успешных вызова, 5-й (summary) —
    # отказ; после него новых вызовов модели быть не должно.
    assert len(provider.calls) == 5

    markdown = render_markdown(report)
    assert report.cards[0].title in markdown


async def test_summary_llm_error_after_retry_does_not_crash_run(settings, brief):
    """summarize бросает после переспроса с увеличенным лимитом токенов —
    тот же честный результат, что и при немедленном отказе."""
    provider = _ScriptedProvider(
        {
            _PLANNER_KEY: [_PLAN_JSON],
            _SCORER_KEY: [_SCORE_JSON],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_TRUNCATED_SUMMARY],  # один и тот же обрыв на каждой попытке
        }
    )
    async with httpx.AsyncClient() as client:
        report = await _pipeline(settings, provider, [_OneHitAdapter(client)]).run(brief, request_id=1)

    assert len(report.cards) == 1
    assert report.summary.recommendation
    assert any("сводка" in caveat.lower() for caveat in report.summary.caveats)
    # 4 успешных + 2 попытки summary (исходная и переспрос с большим лимитом).
    assert len(provider.calls) == 6

    markdown = render_markdown(report)
    assert report.cards[0].title in markdown


# ── Прогресс сообщает фактическое число карточек (критерий 3) ───────────────
async def test_progress_reports_published_and_dropped_card_counts(settings, brief):
    """Две группы: одна даёт честную карточку, другая — пустышку «не найдено».
    В прогрессе видно фактическое число опубликованных и отброшенных."""
    provider = _ScriptedProvider(
        {
            _PLANNER_KEY: [_PLAN_JSON_TWO],
            _SCORER_KEY: [_SCORE_JSON_TWO],
            _CARD_AUTHOR_KEY: [_CARD_JSON, _PLACEHOLDER_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    events: list[tuple[str, str, float]] = []

    async def progress(stage: str, message: str, fraction: float) -> None:
        events.append((stage, message, fraction))

    async with httpx.AsyncClient() as client:
        report = await _pipeline(settings, provider, [_TwoHitAdapter(client)], progress=progress).run(
            brief, request_id=1
        )

    assert len(report.cards) == 1
    card_messages = [message for stage, message, _ in events if stage == "cards"]
    assert any("Готово карточек: 1, отброшено пустых: 1" in message for message in card_messages)


# ── РЕШ-42: verify_card подхватывает контракт проверки ──────────────────────
async def test_verify_card_moves_unsourced_claims_into_card_field(settings):
    """Находки проверки ложатся в SolutionCard.unsourced_claims, а не только
    строкой в risks."""
    verify_json = json.dumps(
        {
            "needs_rework": True,
            "confidence": "low",
            "confidence_note": "число в essence не совпадает с evidence",
            "unsourced_claims": ["обеспечивает срок службы до 30 месяцев"],
            "notes": ["расхождение чисел"],
        },
        ensure_ascii=False,
    )
    provider = _ScriptedProvider({_VERIFIER_KEY: [verify_json]})
    router = LLMRouter(settings, providers={"deepseek": provider})
    card = SolutionCard(
        title="Катализатор",
        essence="Продлевает срок службы.",
        applicability="Закрывает задачу.",
        next_step="Запросить протокол испытаний.",
        evidence=[Evidence(claim="Наработка 14 месяцев", url="https://example.org/a")],
    )

    verified = await stages.verify_card(router, card, [])

    assert verified.unsourced_claims == ["обеспечивает срок службы до 30 месяцев"]
    assert verified.confidence == Confidence.LOW


async def test_verify_card_downgrades_unconfirmed_maturity_to_unknown(settings, brief):
    """Неподтверждённая зрелость → maturity_stage карточки становится unknown
    И отчёт печатает «стадия не определена» (сквозной тест с рендером)."""
    verify_json = json.dumps(
        {
            "needs_rework": True,
            "confidence": "low",
            "confidence_note": "признак стадии слабее заявленного",
            "unsourced_claims": ["стадия зрелости не подтверждена: pilot"],
            "notes": ["в источниках только лабораторные данные"],
        },
        ensure_ascii=False,
    )
    provider = _ScriptedProvider({_VERIFIER_KEY: [verify_json]})
    router = LLMRouter(settings, providers={"deepseek": provider})
    card = SolutionCard(
        title="Катализатор",
        essence="Работает на действующей установке.",
        applicability="Закрывает задачу.",
        next_step="Запросить протокол испытаний.",
        maturity_stage=MaturityStage.PILOT,
        evidence=[Evidence(claim="Испытан на установке", url="https://example.org/a")],
    )

    verified = await stages.verify_card(router, card, [])
    report = Report(
        request_id=1,
        brief=brief,
        cards=[verified],
        summary=ReportSummary(recommendation="Начать с варианта №1."),
        generated_at=datetime.now(UTC),
    )
    text = render_markdown(report)

    assert verified.maturity_stage == MaturityStage.UNKNOWN
    assert "стадия не определена" in text


async def test_requirement_checks_from_card_author_reach_rendered_report(settings, brief):
    """requirement_checks из ответа card_author доезжает до карточки и до
    отрендеренного отчёта (РЕШ-42, сквозной тест: заглушка → рендер)."""
    card_json = _card_json(
        requirement_checks=[
            {
                "requirement": "работа при 340 °C",
                "status": "confirmed",
                "evidence_url": "https://doi.org/10.1/example",
                "note": "",
            },
            {
                "requirement": "сырьё с повышенным содержанием серы",
                "status": "not_confirmed",
                "evidence_url": None,
                "note": "в источниках нет отдельного расчёта под ваше содержание серы",
            },
        ]
    )
    provider = _ScriptedProvider({_CARD_AUTHOR_KEY: [card_json]})
    router = LLMRouter(settings, providers={"deepseek": provider})
    group = [
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title="Катализатор",
                url="https://doi.org/10.1/example",
                org="Институт катализа",
                provider="crossref",
            ),
            relevance=8,
            maturity=6,
            accessibility=7,
            freshness=6,
            completeness=7,
        )
    ]

    card = await stages.author_card(router, brief, group)
    report = Report(
        request_id=1,
        brief=brief,
        cards=[card],
        summary=ReportSummary(recommendation="Начать с варианта №1."),
        generated_at=datetime.now(UTC),
    )
    text = render_markdown(report)

    assert len(card.requirement_checks) == 2
    assert "**Сверка с требованиями задачи:**" in text
    assert "работа при 340 °C: подтверждено источником" in text
    assert "сырьё с повышенным содержанием серы: не подтверждено источником" in text


async def test_initial_caveat_reaches_report_and_rendered_markdown(settings, brief):
    """П-1 аудита: оговорка о том, что случилось до пайплайна (отказ интейка),
    доезжает до `ReportSummary.caveats` и печатается в отчёте — тем же
    механизмом, каким `fallback_summary` доносит до клиента отказ сводки.
    Оговорка встаёт первой: она объясняет качество всей выдачи."""
    provider = _ScriptedProvider(
        {
            _PLANNER_KEY: [_PLAN_JSON],
            _SCORER_KEY: [_SCORE_JSON],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [json.dumps({"comparison_rows": [], "recommendation": "Рассмотреть №1.", "caveats": ["и"]})],
        }
    )
    async with httpx.AsyncClient() as client:
        report = await _pipeline(settings, provider, [_OneHitAdapter(client)]).run(
            brief, request_id=1, initial_caveats=[cli.INTAKE_FALLBACK_CAVEAT]
        )

    assert report.summary.caveats[0] == cli.INTAKE_FALLBACK_CAVEAT
    assert report.summary.caveats[1] == "и", "оговорки самой сводки не должны потеряться"
    assert cli.INTAKE_FALLBACK_CAVEAT in render_markdown(report)


# ── Т-2: устойчивость интейка ────────────────────────────────────────────────
class _RecordingIntakePipeline:
    """Пайплайн-заглушка для проверок `cli._run`: не ходит ни в источники, ни
    в модель — только запоминает бриф и переданный `initial_stage_log`."""

    def __init__(
        self, *, settings=None, router=None, adapters=None, progress=None, session_factory=None, preset_plan=None
    ) -> None:
        # `preset_plan` (Т-19, РЕШ-83) заглушке не нужен, но конструктор обязан
        # его принимать: `cli._run` передаёт его всегда, а проверки этого файла
        # про интейк, не про план поиска.
        del settings, router, adapters, progress, session_factory, preset_plan

    async def run(
        self,
        brief: Brief,
        *,
        request_id: int = 0,
        raw_text: str = "",
        initial_stage_log=None,
        initial_caveats=None,
        constraints_unresolved: bool = False,
    ) -> Report:
        del constraints_unresolved
        _RecordingIntakePipeline.captured.append(brief)
        _RecordingIntakePipeline.stage_logs.append(initial_stage_log)
        _RecordingIntakePipeline.caveats.append(initial_caveats)
        return Report(
            request_id=request_id,
            brief=brief,
            cards=[],
            summary=ReportSummary(recommendation="тест: пайплайн подменён заглушкой"),
            generated_at=datetime.now(UTC),
        )

    async def aclose(self) -> None:
        pass


def _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory) -> None:
    """Общая подготовка для тестов `cli._run` в этом файле: подмена настроек,
    роутера, схемы БД и тяжёлого пайплайна — сеть и боевой data/scout.db не
    затрагиваются."""
    import app.pipeline as pipeline_pkg

    _RecordingIntakePipeline.captured = []
    _RecordingIntakePipeline.stage_logs = []
    _RecordingIntakePipeline.caveats = []
    monkeypatch.setattr(pipeline_pkg, "ScoutPipeline", _RecordingIntakePipeline)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    router = LLMRouter(settings=settings, providers={"gigachat": fake_provider_factory("gigachat")})
    monkeypatch.setattr(cli, "LLMRouter", lambda _settings: router)

    async def fake_init_db() -> None:
        pass

    monkeypatch.setattr("app.db.init_db", fake_init_db)


def _intake_with_one_question(draft: Brief):
    """Черновик интейка с одним уточняющим вопросом — общая заготовка."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(
            questions=[IntakeQuestion(question="Какая температура процесса?", why="нужна для отбора решений")],
            draft_brief=draft,
        )

    return fake_run_intake


async def test_refine_brief_failure_keeps_owner_answers_in_own_field(
    monkeypatch, settings, fake_provider_factory, tmp_path
):
    """Отказ `refine_brief` не роняет прогон и не теряет ответы владельца — они
    ложатся в `Brief.owner_answers` (РЕШ-58, П-2 аудита), а `constraints`
    черновика остаются нетронутыми: пара «вопрос — ответ» печатается клиенту
    строкой «Ограничения:» и разворачивается в `requirement_checks` каждой
    карточки, а ограничением задачи она не является."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)
    draft = Brief(
        industry="нефтепереработка",
        task_summary="Катализатор гидроочистки теряет активность раньше срока.",
        constraints=["работа при 340 °C"],
    )

    async def broken_refine_brief(router, task, answers, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.pipeline.stages.run_intake", _intake_with_one_question(draft))
    monkeypatch.setattr("app.pipeline.stages.refine_brief", broken_refine_brief)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "380 °C")

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", True)

    assert len(_RecordingIntakePipeline.captured) == 1
    brief = _RecordingIntakePipeline.captured[0]
    assert brief.constraints == ["работа при 340 °C"], "ответы владельца не имеют права попадать в ограничения задачи"
    assert [(item.question, item.answer) for item in brief.owner_answers] == [("Какая температура процесса?", "380 °C")]

    # З-8: успешный run_intake тоже оставляет строку журнала — перед отказавшим
    # уточнением здесь запись `intake` без ошибки (сама стадия интейка прошла),
    # затем `intake_refine` с причиной отказа.
    stage_log = _RecordingIntakePipeline.stage_logs[-1]
    assert stage_log is not None and len(stage_log) == 2
    assert stage_log[0].stage == "intake" and stage_log[0].error is None
    assert stage_log[1].stage == "intake_refine" and stage_log[1].error


async def test_refine_brief_failure_tells_the_client_the_brief_stayed_draft(
    monkeypatch, settings, fake_provider_factory, tmp_path
):
    """Доводка П-1: отказ уточнения тоже обязан доехать до отчёта — раньше в
    этой ветке оговорки не появлялось вовсе. Текст `INTAKE_FALLBACK_CAVEAT`
    здесь неверен: он говорит «бриф составлен из текста задачи», а бриф взят из
    черновика интейка — поэтому у ветки своя формулировка."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)
    draft = Brief(industry="нефтепереработка", task_summary="Катализатор гидроочистки теряет активность раньше срока.")

    async def broken_refine_brief(router, task, answers, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.pipeline.stages.run_intake", _intake_with_one_question(draft))
    monkeypatch.setattr("app.pipeline.stages.refine_brief", broken_refine_brief)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "380 °C")

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", True)

    assert _RecordingIntakePipeline.caveats[-1] == [cli.REFINE_FALLBACK_CAVEAT]


async def test_successful_refine_brief_adds_no_caveat(monkeypatch, settings, fake_provider_factory, tmp_path):
    """Обратная сторона: когда уточнение отработало, оговорке взяться неоткуда —
    иначе клиент читал бы про черновую постановку на здоровом прогоне."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)
    draft = Brief(industry="нефтепереработка", task_summary="Катализатор гидроочистки теряет активность раньше срока.")

    async def fake_refine_brief(router, task, answers, *, industry_hint="не указана"):
        return draft.model_copy(update={"task_summary": "Уточнённая постановка."})

    monkeypatch.setattr("app.pipeline.stages.run_intake", _intake_with_one_question(draft))
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fake_refine_brief)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "380 °C")

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", True)

    assert _RecordingIntakePipeline.caveats[-1] == []


async def test_intake_and_refine_failures_are_told_apart_in_stage_log(
    monkeypatch, settings, fake_provider_factory, tmp_path
):
    """Совет С-2 аудита: строка журнала обязана различать два разных отказа
    интейка. Отказ разбора текста пишется стадией `intake`, отказ уточнения
    брифа — `intake_refine`; одинаковая строка не давала разбору инцидента
    понять, какая из двух стадий отказала."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)
    draft = Brief(industry="нефтепереработка", task_summary="Катализатор гидроочистки теряет активность раньше срока.")

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    async def broken_refine_brief(router, task, answers, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "380 °C")
    await cli._run("сырой текст задачи", None, None, tmp_path / "report-a.md", True)

    monkeypatch.setattr("app.pipeline.stages.run_intake", _intake_with_one_question(draft))
    monkeypatch.setattr("app.pipeline.stages.refine_brief", broken_refine_brief)
    await cli._run("сырой текст задачи", None, None, tmp_path / "report-b.md", True)

    # З-8: успешный интейк второго прогона тоже оставляет запись, поэтому
    # различать отказ надо по стадии, которая несёт ошибку, а не по первой
    # строке журнала — во втором прогоне она теперь `intake` (успех), а отказ
    # уточнения — второй строкой `intake_refine`.
    failed_stages = [
        next(record.stage for record in log if record.error) for log in _RecordingIntakePipeline.stage_logs
    ]
    assert failed_stages == ["intake", "intake_refine"]


async def test_failed_intake_stage_carries_actual_cost_not_zero(monkeypatch, settings, fake_provider_factory, tmp_path):
    """П-4 аудита: строка `intake` в журнале обязана нести снимок счётчика, а
    не ноль. Стадия платная, и отказавший интейк тратится до отказа:
    `complete_json` делает оплаченный вызов, затем оплаченный ремонтный при
    несходимости со схемой и только потом поднимает ошибку.

    Здесь работает настоящий `run_intake` на провайдере-заглушке, который
    отвечает не-JSON: два оплаченных вызова по 0,01 ₽ и отказ."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", False)

    stage_log = _RecordingIntakePipeline.stage_logs[-1]
    assert stage_log is not None and len(stage_log) == 1
    record = stage_log[0]
    assert record.error, "отказ интейка обязан оставлять причину"
    assert record.cost_rub == pytest.approx(0.02), "две оплаченные попытки по 0,01 ₽ — журнал обязан их показать"
    assert record.provider == "gigachat", "провайдер, который потратил деньги, обязан быть назван"


@pytest.mark.parametrize("typed", ["не знаю", "  Не Знаю  "])
async def test_unknown_answer_is_recorded_as_unspecified_requirement(
    monkeypatch, settings, fake_provider_factory, tmp_path, typed
):
    """Распоряжение владельца (РЕШ-58): ответ «не знаю» — а это умолчание
    `typer.prompt`, которое получает любой нажавший Enter, — записывается в
    бриф как «требование не специализировано». Сравнение по очищенному
    значению и без учёта регистра."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)
    draft = Brief(industry="нефтепереработка", task_summary="Катализатор гидроочистки теряет активность раньше срока.")

    async def broken_refine_brief(router, task, answers, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.pipeline.stages.run_intake", _intake_with_one_question(draft))
    monkeypatch.setattr("app.pipeline.stages.refine_brief", broken_refine_brief)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: typed)

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", True)

    answers = _RecordingIntakePipeline.captured[0].owner_answers
    assert [item.answer for item in answers] == ["требование не специализировано"]


async def test_owner_answers_reach_planner_and_scorer_prompts(settings):
    """Поле, которое никуда не доходит, хуже прежнего состояния: ответы
    владельца обязаны быть видны модели на обеих стадиях, где бриф подаётся
    по-разному — целиком (`plan_search`, BRIEF_JSON) и по полям (`score_candidates`)."""
    brief_with_answers = Brief(
        industry="нефтепереработка",
        task_summary="Катализатор гидроочистки теряет активность раньше срока.",
        owner_answers=[OwnerAnswer(question="Какая температура процесса?", answer="380 °C")],
    )
    provider = _ScriptedProvider({_PLANNER_KEY: [_PLAN_JSON], _SCORER_KEY: [_SCORE_JSON]})
    router = LLMRouter(settings, providers={"deepseek": provider})
    candidate = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Катализатор",
        url="https://doi.org/10.1/example",
        provider="crossref",
    )

    await stages.plan_search(router, brief_with_answers)
    await stages.score_candidates(router, brief_with_answers, [candidate])

    planner_text = provider.calls[0][0].content
    assert "Какая температура процесса?" in planner_text and "380 °C" in planner_text

    scorer_text = provider.calls[1][1].content
    assert "Какая температура процесса?" in scorer_text and "380 °C" in scorer_text


async def test_intake_stage_error_reaches_pipeline_logs_with_drafted_status(
    settings, brief, fake_provider_factory, db_session_factory
):
    """Отказавшая стадия интейка, переданная оркестратору через
    `initial_stage_log`, попадает в `pipeline_logs` строкой `intake` с
    непустым `error`; статус запроса остаётся `drafted` — прогон дошёл до
    отчёта, а не свалился в `failed`."""
    provider = fake_provider_factory("deepseek", [_PLAN_JSON, _SCORE_JSON, _CARD_JSON, _VERIFY_JSON, _SUMMARY_JSON])
    intake_failure = StageRecord(
        stage="intake",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        error="интейк недоступен: провайдер вернул 503",
    )
    async with httpx.AsyncClient() as client:
        pipeline = _pipeline(settings, provider, [_OneHitAdapter(client)], session_factory=db_session_factory)
        report = await pipeline.run(
            brief, request_id=1, raw_text="текст с отказавшим интейком", initial_stage_log=[intake_failure]
        )

    assert len(report.cards) == 1

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1
        assert requests[0].status == "drafted"

        intake_logs = (await session.execute(select(PipelineLog).where(PipelineLog.stage == "intake"))).scalars().all()
        assert len(intake_logs) == 1
        assert intake_logs[0].error


class _CostlyProvider(LLMProvider):
    """Провайдер, у которого один вызов дороже всего лимита запроса.

    Нужен, чтобы стоп-кран срабатывал на настоящем стеке (`CostTracker.add`
    внутри `LLMRouter.complete`), а не подменой самой стадии: проверяется не
    порядок `except` в `cli.py`, а то, что после уже состоявшейся траты
    остаётся запись в БД."""

    name = "gigachat"
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
        return LLMResult(
            text="{}",
            usage=Usage(tokens_in=10, tokens_out=10, cost_rub=99.0, provider=self.name, model="fake"),
        )

    async def health(self) -> tuple[bool, str]:
        return True, "заглушка"


def _patch_costly_router(monkeypatch, settings) -> None:
    """Роутер, на котором первый же вызов модели выбирает лимит стоимости."""
    router = LLMRouter(settings, providers={"gigachat": _CostlyProvider()})
    monkeypatch.setattr(cli, "LLMRouter", lambda _settings: router)


async def test_cost_limit_on_intake_leaves_failed_run_with_actual_cost(
    monkeypatch, settings, fake_provider_factory, db_session_factory, tmp_path
):
    """П-5 аудита: стоп-кран на интейке обязан оставить след. Раньше
    `CostLimitExceededError` уходила наружу до конструирования пайплайна —
    `persist_run` не вызывался, `requests` не получал даже строки `failed`,
    отчёта не было, а деньги были потрачены. Исключение по-прежнему уходит
    наружу — стоп-кран не смягчается."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)
    _patch_costly_router(monkeypatch, settings)
    monkeypatch.setattr("app.db.session_scope", db_session_factory)

    with pytest.raises(CostLimitExceededError):
        await cli._run("текст, который упрётся в лимит на интейке", None, None, tmp_path / "report.md", False)

    assert _RecordingIntakePipeline.captured == [], "прогон обязан упасть, а не пойти дальше на обеднённом брифе"

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1, "оборванный стоп-краном прогон обязан оставить строку в requests"
        assert requests[0].status == "failed"
        assert requests[0].cost_rub == pytest.approx(99.0), "потраченные деньги обязаны попасть в запись"
        assert requests[0].error

        logs = (await session.execute(select(PipelineLog))).scalars().all()
        assert [(row.stage, row.cost_rub) for row in logs] == [("intake", pytest.approx(99.0))]
        assert logs[0].error


async def test_cost_limit_on_refine_brief_leaves_failed_run_with_actual_cost(
    monkeypatch, settings, fake_provider_factory, db_session_factory, tmp_path
):
    """Зеркало для второго места вызова — более достижимый случай: лимит
    выбирается уже на уточнении брифа. Строка журнала при этом обязана
    называться `intake_refine` (С-2), а не `intake`."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)
    _patch_costly_router(monkeypatch, settings)
    monkeypatch.setattr("app.db.session_scope", db_session_factory)
    draft = Brief(industry="нефтепереработка", task_summary="Катализатор гидроочистки теряет активность раньше срока.")
    monkeypatch.setattr("app.pipeline.stages.run_intake", _intake_with_one_question(draft))
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "380 °C")

    with pytest.raises(CostLimitExceededError):
        await cli._run("текст, который упрётся в лимит на уточнении", None, None, tmp_path / "report.md", True)

    assert _RecordingIntakePipeline.captured == []

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1
        assert requests[0].status == "failed"
        assert requests[0].cost_rub == pytest.approx(99.0)

        # З-8: успешный run_intake (здесь бесплатный - заглушка не зовёт
        # роутер вовсе) тоже оставляет строку журнала перед стоп-краном
        # уточнения.
        logs = (await session.execute(select(PipelineLog))).scalars().all()
        assert [(row.stage, row.cost_rub) for row in logs] == [
            ("intake", 0.0),
            ("intake_refine", pytest.approx(99.0)),
        ]
        assert logs[0].error is None
        assert logs[1].error


async def test_cost_limit_exceeded_during_intake_propagates_and_run_fails(
    monkeypatch, settings, fake_provider_factory, db_session_factory, tmp_path
):
    """РЕШ-45: `CostLimitExceededError` на интейке — команда «стоп тратить»,
    не отказ модели. `cli._run` обязан пробросить исключение, а не подменить
    бриф запасным и продолжить прогон.

    Изоляция от боевого `.env` (З-1): без подмены `app.db.session_scope`
    запись об отказавшем прогоне ушла бы через реальный `session_scope`, то
    есть мимо тестовой базы — образец соседних тестов выше
    (`test_cost_limit_on_intake_leaves_failed_run_with_actual_cost`)."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)
    monkeypatch.setattr("app.db.session_scope", db_session_factory)

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        raise CostLimitExceededError("запрос израсходовал 10.00 ₽ при лимите 10.00 ₽")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)

    with pytest.raises(CostLimitExceededError):
        await cli._run("текст, который упрётся в лимит на интейке", None, None, tmp_path / "report.md", False)

    assert _RecordingIntakePipeline.captured == []

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1, "оборванный стоп-краном прогон обязан оставить строку в тестовой базе"
        assert requests[0].status == "failed"
        assert requests[0].error


async def test_cost_limit_exceeded_during_refine_brief_propagates_and_run_fails(
    monkeypatch, settings, fake_provider_factory, db_session_factory, tmp_path
):
    """Зеркало предыдущего теста для второго места вызова: лимит стоимости
    выбран уже на уточнении брифа. Ветка `refine_brief` обязана вести себя так
    же, как `run_intake` (РЕШ-45): пробросить `CostLimitExceededError`, а не
    подменить её на черновик интейка с ответами владельца и пойти дальше.

    Изоляция от боевого `.env` (З-1): подмена `app.db.session_scope`, как у
    соседних тестов, — без неё запись легла бы мимо тестовой базы."""
    _patch_cli_for_intake_test(monkeypatch, settings, fake_provider_factory)
    monkeypatch.setattr("app.db.session_scope", db_session_factory)
    draft = Brief(industry="нефтепереработка", task_summary="Катализатор гидроочистки теряет активность раньше срока.")

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(
            questions=[IntakeQuestion(question="Какая температура процесса?", why="нужна для отбора решений")],
            draft_brief=draft,
        )

    async def broken_refine_brief(router, task, answers, *, industry_hint="не указана"):
        raise CostLimitExceededError("запрос израсходовал 10.00 ₽ при лимите 10.00 ₽")

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", broken_refine_brief)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "380 °C")

    with pytest.raises(CostLimitExceededError):
        await cli._run("текст, который упрётся в лимит на уточнении", None, None, tmp_path / "report.md", True)

    assert _RecordingIntakePipeline.captured == []

    async with db_session_factory() as session:
        requests = (await session.execute(select(RequestRow))).scalars().all()
        assert len(requests) == 1, "оборванный стоп-краном прогон обязан оставить строку в тестовой базе"
        assert requests[0].status == "failed"
        assert requests[0].error
