"""Семантический судья дублей (Д-6 волны Э2-З, РЕШ-152).

Сети нет: провайдер модели — `FakeProvider` из `conftest.py` (инвариант №7).
Три уровня проверки: (1) чистая функция `judge_duplicate_groups` на подменах
роутера — успех, отказ провайдера, кривой JSON, стоп-кран; (2) чистая функция
`apply_judge_groups` — склейка заготовок без задваивания кандидата;
(3) сквозной прогон `ScoutPipeline.run`, показывающий, что вердикт судьи
реально доезжает до карточек и что его отказ не роняет прогон.
"""

from __future__ import annotations

import httpx
import pytest

from app.llm import CostLimitExceededError, LLMRouter
from app.pipeline import stages
from app.pipeline.dedup_judge import (
    JUDGE_CANDIDATE_LIMIT,
    JUDGE_MAX_GROUP_SIZE,
    _sanitize,
    apply_judge_groups,
    judge_duplicate_groups,
)
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import (
    Brief,
    Candidate,
    Evidence,
    ReportSummary,
    ScoredCandidate,
    SearchPlan,
    SearchQuery,
    SolutionCard,
    SourceType,
)
from app.search.base import SearchAdapter


def _candidate(number: int, *, org: str | None = None) -> Candidate:
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=f"Разработка №{number}",
        url=f"https://example.org/dev/{number}",
        org=org,
        snippet=f"Фрагмент разработки номер {number}.",
        provider="crossref",
    )


def _scored(candidate: Candidate, *, relevance: int = 8) -> ScoredCandidate:
    return ScoredCandidate(
        candidate=candidate, relevance=relevance, maturity=6, accessibility=6, freshness=6, completeness=6
    )


# ── judge_duplicate_groups: чистая функция на подменах роутера ──────────────


async def test_меньше_двух_кандидатов_вызова_модели_не_делает(settings, fake_provider_factory):
    """Дублировать нечего — модель не спрашиваем вовсе (страховка от лишней траты)."""
    provider = fake_provider_factory("deepseek")
    router = LLMRouter(settings, providers={"deepseek": provider})

    result = await judge_duplicate_groups(router, [_scored(_candidate(1))])

    assert result is None
    assert provider.calls == []


async def test_успешный_вердикт_разбирается_в_группы_номеров(settings, fake_provider_factory):
    provider = fake_provider_factory("deepseek", ['{"duplicate_groups": [[1, 2]]}'])
    router = LLMRouter(settings, providers={"deepseek": provider})
    candidates = [_scored(_candidate(1)), _scored(_candidate(2)), _scored(_candidate(3))]

    result = await judge_duplicate_groups(router, candidates)

    assert result == [[1, 2]]


async def test_пустой_вердикт_это_пустой_список_а_не_none(settings, fake_provider_factory):
    """Модель ответила, но дублей не нашла — это успех с пустым результатом,
    а не деградация: разница важна для строки лога и для отчёта прогона."""
    provider = fake_provider_factory("deepseek", ['{"duplicate_groups": []}'])
    router = LLMRouter(settings, providers={"deepseek": provider})
    candidates = [_scored(_candidate(1)), _scored(_candidate(2))]

    result = await judge_duplicate_groups(router, candidates)

    assert result == []


async def test_отказ_провайдера_деградирует_до_none(settings, fake_provider_factory):
    """Провайдер отказывает на каждой попытке — модель считается недоступной,
    а не роняет вызывающий код (мутация «а»: без этой деградации падение
    провайдера уронило бы весь прогон)."""
    provider = fake_provider_factory("deepseek", fail_times=5)
    router = LLMRouter(settings, providers={"deepseek": provider})
    candidates = [_scored(_candidate(1)), _scored(_candidate(2))]

    result = await judge_duplicate_groups(router, candidates)

    assert result is None


async def test_кривой_json_после_починки_тоже_деградирует_до_none(settings, fake_provider_factory):
    """Ответ не JSON вовсе, роутер один раз просит починить — чинить нечего,
    ответ повторяется, и после исчерпания попыток `judge_duplicate_groups`
    отдаёт `None`, а не пробрасывает исключение роутера дальше."""
    provider = fake_provider_factory("deepseek", ["точно не JSON, а извинение текстом"])
    router = LLMRouter(settings, providers={"deepseek": provider})
    candidates = [_scored(_candidate(1)), _scored(_candidate(2))]

    result = await judge_duplicate_groups(router, candidates)

    assert result is None


async def test_стоп_кран_по_стоимости_пробрасывается_а_не_гасится(settings, fake_provider_factory):
    """`CostLimitExceededError` — команда «стоп тратить» (РЕШ-45), а не отказ
    модели: широкий `except Exception` в деградации не имеет права её съесть."""
    tight = settings.model_copy(update={"cost_limit_rub_per_request": 0.005})
    provider = fake_provider_factory("deepseek", ['{"duplicate_groups": []}'])
    router = LLMRouter(tight, providers={"deepseek": provider})
    candidates = [_scored(_candidate(1)), _scored(_candidate(2))]

    with pytest.raises(CostLimitExceededError):
        await judge_duplicate_groups(router, candidates)


async def test_кандидаты_сверх_лимита_в_вызов_не_попадают(settings, fake_provider_factory):
    """Судья видит не больше `JUDGE_CANDIDATE_LIMIT` кандидатов — листинг в
    системном сообщении обязан содержать ровно этот номер как последний."""
    provider = fake_provider_factory("deepseek", ['{"duplicate_groups": []}'])
    router = LLMRouter(settings, providers={"deepseek": provider})
    candidates = [_scored(_candidate(n)) for n in range(1, JUDGE_CANDIDATE_LIMIT + 5)]

    await judge_duplicate_groups(router, candidates)

    system_text = provider.calls[0][0].content
    assert f"{JUDGE_CANDIDATE_LIMIT}. Разработка" in system_text
    assert f"{JUDGE_CANDIDATE_LIMIT + 1}. Разработка" not in system_text


# ── _sanitize: судья не склеивает всё в одну кучу (мутация «б») ─────────────


def test_группа_больше_предела_отбрасывается():
    """Если бы предела не было, эта группа прошла бы целиком — ровно та
    мутация, которую обязана ловить проверка."""
    huge_group = list(range(1, JUDGE_MAX_GROUP_SIZE + 2))  # на единицу больше предела
    assert len(huge_group) > JUDGE_MAX_GROUP_SIZE

    result = _sanitize([huge_group], size=JUDGE_CANDIDATE_LIMIT)

    assert result == []


def test_группа_ровно_на_пределе_принимается():
    """Верхняя граница не строгая: ровно `JUDGE_MAX_GROUP_SIZE` — ещё годная группа."""
    edge_group = list(range(1, JUDGE_MAX_GROUP_SIZE + 1))

    result = _sanitize([edge_group], size=JUDGE_CANDIDATE_LIMIT)

    assert result == [edge_group]


def test_одиночка_без_пары_отбрасывается():
    assert _sanitize([[1]], size=5) == []


def test_номера_вне_диапазона_отфильтровываются_а_не_роняют_разбор():
    assert _sanitize([[1, 2, 99]], size=5) == [[1, 2]]


def test_пересекающиеся_группы_вторая_отбрасывается():
    result = _sanitize([[1, 2], [2, 3]], size=5)
    assert result == [[1, 2]]


# ── apply_judge_groups: склейка без задваивания кандидата ───────────────────


def test_склейка_объединяет_заготовки_без_дублирования_кандидата():
    """Наивная склейка (просто дописать группу судьи в общий список и снова
    прогнать через `merge_duplicate_groups`) оставляет кандидата в ДВУХ
    заготовках сразу — этот тест ловит именно такую регрессию."""
    a, b, c = _scored(_candidate(1)), _scored(_candidate(2)), _scored(_candidate(3))
    groups = [[a], [b], [c]]

    merged = apply_judge_groups(groups, [[1, 3]], judge_input=[a, b, c])

    assert len(merged) == 2
    sizes = sorted(len(group) for group in merged)
    assert sizes == [1, 2]
    # Кандидат встречается ровно один раз суммарно по всем группам.
    flat = [item for group in merged for item in group]
    assert flat.count(a) == 1
    assert flat.count(c) == 1
    merged_with_a = next(group for group in merged if a in group)
    assert c in merged_with_a


def test_пустой_вердикт_судьи_не_меняет_список_групп():
    a, b = _scored(_candidate(1)), _scored(_candidate(2))
    groups = [[a], [b]]

    assert apply_judge_groups(groups, [], judge_input=[a, b]) == groups


def test_кандидат_вне_заготовок_group_for_cards_молча_пропускается():
    """Номер судьи может указывать на кандидата, отсечённого `group_for_cards`
    до целевого числа заготовок (`target`) — склейка не падает, просто нечего
    сливать с этой стороны."""
    a, b = _scored(_candidate(1)), _scored(_candidate(2))
    groups = [[a]]  # b в заготовки не попал

    merged = apply_judge_groups(groups, [[1, 2]], judge_input=[a, b])

    assert merged == [[a]]


# ── Сквозной прогон: вердикт судьи доезжает до карточек, отказ — не роняет ──


class _MultiOrgAdapter(SearchAdapter):
    """Три кандидата трёх разных организаций — три отдельные заготовки до
    судьи (`group_for_cards` группирует по организации)."""

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        return [
            _candidate(1, org="Институт А"),
            _candidate(2, org="Институт Б"),
            _candidate(3, org="Институт В"),
        ]


_PLAN = SearchPlan(
    queries=[SearchQuery(text="катализатор гидроочистки", source_type=SourceType.PUBLICATION, lang="ru", rationale="т")]
)


@pytest.fixture
def stub_non_judge_stages(monkeypatch: pytest.MonkeyPatch):
    """Все LLM-стадии, кроме судьи, — заглушки: тест про подключение судьи к
    пайплайну, а не про качество карточек (та же граница, что у
    `tests/test_diversity.py`)."""
    calls: list[list] = []

    async def fake_plan(router, brief: Brief) -> SearchPlan:
        return _PLAN

    async def fake_score(router, brief: Brief, candidates: list[Candidate], **kwargs) -> list[ScoredCandidate]:
        return [_scored(c) for c in candidates]

    async def fake_author(router, brief: Brief, group: list[ScoredCandidate]) -> SolutionCard:
        calls.append(group)
        return SolutionCard(
            title=group[0].candidate.title,
            essence="Суть решения одной строкой.",
            applicability="Закрывает часть задачи из брифа.",
            next_step="Запросить протокол испытаний.",
            evidence=[Evidence(claim="Факт из источника", url=group[0].candidate.url)],
            candidate_urls=[item.candidate.url for item in group],
        )

    async def fake_verify(router, card: SolutionCard, group=None) -> SolutionCard:
        return card

    async def fake_summary(router, brief: Brief, cards: list[SolutionCard]) -> ReportSummary:
        return ReportSummary(recommendation="Начать с первого варианта.")

    monkeypatch.setattr(stages, "plan_search", fake_plan)
    monkeypatch.setattr(stages, "score_candidates", fake_score)
    monkeypatch.setattr(stages, "author_card", fake_author)
    monkeypatch.setattr(stages, "verify_card", fake_verify)
    monkeypatch.setattr(stages, "summarize", fake_summary)
    return calls


async def _run(settings, fake_provider_factory, brief, *, judge_responses=None, judge_fail_times=0):
    provider = fake_provider_factory("deepseek", judge_responses, fail_times=judge_fail_times)
    router = LLMRouter(settings, providers={"deepseek": provider})
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[_MultiOrgAdapter(client)])
        report = await pipeline.run(brief, request_id=1, raw_text="узи свиноматок супоросность")
    return report, provider


async def test_вердикт_судьи_доезжает_до_карточек(settings, brief, fake_provider_factory, stub_non_judge_stages):
    """Судья говорит «кандидаты 1 и 2 — одна разработка» → в отчёте на одну
    карточку меньше, а объединённая карточка ссылается на обоих (мутация «б»
    наоборот: игнорирование вердикта судьи держало бы карточек три)."""
    report, _ = await _run(settings, fake_provider_factory, brief, judge_responses=['{"duplicate_groups": [[1, 2]]}'])

    assert len(report.cards) == 2
    urls = {str(url) for card in report.cards for url in card.candidate_urls}
    assert str(_candidate(1).url) in urls
    assert str(_candidate(2).url) in urls
    merged_card = next(card for card in report.cards if len(card.candidate_urls) == 2)
    assert {str(u) for u in merged_card.candidate_urls} == {str(_candidate(1).url), str(_candidate(2).url)}


async def test_отказ_судьи_не_роняет_прогон(settings, brief, fake_provider_factory, stub_non_judge_stages):
    """Провайдер отказывает на каждой попытке — прогон всё равно доходит до
    отчёта, карточек ровно три (без семантической склейки, как раньше)."""
    report, _provider = await _run(settings, fake_provider_factory, brief, judge_fail_times=10)

    assert len(report.cards) == 3
    assert report.summary.recommendation


async def test_пустой_вердикт_судьи_карточек_не_меняет(settings, brief, fake_provider_factory, stub_non_judge_stages):
    report, _ = await _run(settings, fake_provider_factory, brief, judge_responses=['{"duplicate_groups": []}'])

    assert len(report.cards) == 3
