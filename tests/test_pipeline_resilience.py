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
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from app.export.markdown import render_markdown
from app.llm import LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import (
    Candidate,
    Confidence,
    Evidence,
    MaturityStage,
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


def _pipeline(settings, provider: LLMProvider, adapters: list[SearchAdapter], *, progress=None) -> ScoutPipeline:
    router = LLMRouter(settings, providers={"deepseek": provider})
    return ScoutPipeline(settings=settings, router=router, adapters=adapters, progress=progress)


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
