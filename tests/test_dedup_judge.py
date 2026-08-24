"""Семантический судья дублей (Д-6 волны Э2-З, РЕШ-152).

Сети нет: провайдер модели — `FakeProvider` из `conftest.py` (инвариант №7).
Четыре уровня проверки: (1) чистая функция `judge_duplicate_groups` на
подменах роутера — успех, отказ провайдера, кривой JSON, стоп-кран;
(2) чистая функция `apply_judge_groups` — склейка заготовок без задваивания
кандидата; (3) сквозной прогон `ScoutPipeline.run`, показывающий, что вердикт
судьи реально доезжает до карточек и что его отказ не роняет прогон;
(4) склейка ДО среза до `target` (РЕШ-181 п. 4, цикл правок пост-аудита
волны Э2-З) — и на чистых функциях, и сквозным прогоном.
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
    describe_groups,
    judge_duplicate_groups,
)
from app.pipeline.orchestrator import ScoutPipeline
from app.pipeline.selection import cut_to_target, group_candidates
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
    сливать с этой стороны. Это описывает поведение `apply_judge_groups` саму
    по себе (в изоляции), а не порядок вызовов в `run_stages.py` — тот теперь
    зовёт склейку ДО такого среза именно затем, чтобы этот случай на боевых
    данных не наступал (см. тест ниже)."""
    a, b = _scored(_candidate(1)), _scored(_candidate(2))
    groups = [[a]]  # b в заготовки не попал

    merged = apply_judge_groups(groups, [[1, 2]], judge_input=[a, b])

    assert merged == [[a]]


# ── describe_groups: строка журнала о вкладе судьи (правка 2 пост-аудита Э2-З) ──


def test_describe_groups_пустой_вердикт():
    assert describe_groups([], []) == "групп нет"


def test_describe_groups_называет_номера_и_укороченные_заголовки():
    a, b = _scored(_candidate(1, org="Институт А")), _scored(_candidate(2, org="Институт Б"))

    text = describe_groups([[1, 2]], [a, b])

    assert "№1" in text and "№2" in text
    assert "Разработка №1" in text  # заголовок короткий, укорачивать нечего
    assert text.count("(") == 1  # одна группа — одни скобки


def test_describe_groups_укорачивает_длинный_заголовок():
    long_title = "Способ регенерации катализатора гидроочистки дизельного топлива на модифицированном носителе"
    candidate = Candidate(
        source_type=SourceType.PUBLICATION,
        title=long_title,
        url="https://example.org/dev/1",
        snippet="",
        provider="crossref",
    )
    other = _scored(_candidate(2))
    item = _scored(candidate)

    text = describe_groups([[1, 2]], [item, other])

    assert "…" in text
    assert long_title not in text  # полный заголовок в журнал не уезжает


# ── Судья ДО среза до target (РЕШ-181 п. 4) ──────────────────────────────────


def test_склейка_до_среза_сохраняет_кандидата_за_старой_границей():
    """Живой пример аудита волны Э2-З: пара №1062/№1069 (5.6 и 5.55) — один
    кандидат внутри старого среза до `target`, второй сразу за границей,
    судья считает их дублями. Порядок «срез, потом склейка» (было до
    РЕШ-181 п. 4) отправляет второго под срез не спрошенным — склейка не
    находит для него владельца и молча пропускает (предыдущий тест). Порядок
    «склейка, потом срез» (стало — `run_stages.py`) обязан сохранить обоих
    кандидатов в одной заготовке."""
    scored = [_scored(_candidate(n, org=f"Институт {n}"), relevance=11 - n) for n in range(1, 9)]
    judge_groups = [[6, 7]]  # ранги 6 и 7 — ровно на границе среза до target=6
    target = 6

    old_groups = apply_judge_groups(stages.group_for_cards(scored, target=target), judge_groups, judge_input=scored)
    merged_before_cut = apply_judge_groups(group_candidates(scored), judge_groups, judge_input=scored)
    new_groups = cut_to_target(merged_before_cut, target=target)

    old_urls = {item.candidate.url for group in old_groups for item in group}
    new_urls = {item.candidate.url for group in new_groups for item in group}
    rank7_url = scored[6].candidate.url  # 0-индексация Python, ранг 7 — седьмой по счёту

    assert len(old_groups) == len(new_groups) == target
    assert rank7_url not in old_urls, "старый порядок (срез до склейки) обязан терять кандидата за границей"
    assert rank7_url in new_urls, "новый порядок (склейка до среза) обязан сохранить его в заготовке"


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


# ── Сквозной прогон: судья ДО среза (РЕШ-181 п. 4), эффект на реальной обвязке ──


class _EightRankedAdapter(SearchAdapter):
    """Восемь кандидатов разных организаций и провайдеров — по одному на
    провайдера, чтобы потолок `_pick_top` по одному источнику (`selection.py`)
    не срезал часть выдачи раньше, чем до неё доберётся судья."""

    name = "ranked"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        return [
            Candidate(
                source_type=SourceType.PUBLICATION,
                title=f"Разработка №{n}",
                url=f"https://example.org/dev/{n}",
                org=f"Институт {n}",
                snippet=f"Фрагмент разработки номер {n}.",
                provider=f"provider-{n}",
            )
            for n in range(1, 9)
        ]


async def test_судья_до_среза_сохраняет_кандидата_за_границей_сквозным_прогоном(
    settings, brief, fake_provider_factory, monkeypatch
):
    """Тот же эффект, что и на чистых функциях выше, но через реальную обвязку
    `_run_stages` (`run_stages.py`): ловит мутацию, которая вернула бы срез до
    `target` перед склейкой вердикта судьи (РЕШ-181 п. 4, совет аудита волны
    Э2-З — живой пример пары №1062/№1069, 5.6 и 5.55). Ранг кандидата задаётся
    номером в заголовке и убывающим `relevance`; судья считает ранги 6 и 7
    (граница старого среза при `target_cards = 6`) одной разработкой."""

    async def fake_plan(router, brief: Brief) -> SearchPlan:
        return _PLAN

    async def fake_score(router, brief: Brief, candidates: list[Candidate], **kwargs) -> list[ScoredCandidate]:
        scores = []
        for c in candidates:
            rank = int(str(c.url).rstrip("/").rsplit("/", 1)[-1])
            scores.append(
                ScoredCandidate(
                    candidate=c, relevance=11 - rank, maturity=6, accessibility=6, freshness=6, completeness=6
                )
            )
        return scores

    async def fake_author(router, brief: Brief, group: list[ScoredCandidate]) -> SolutionCard:
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

    provider = fake_provider_factory("deepseek", ['{"duplicate_groups": [[6, 7]]}'])
    router = LLMRouter(settings, providers={"deepseek": provider})
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[_EightRankedAdapter(client)])
        report = await pipeline.run(brief, request_id=1, raw_text="узи свиноматок супоросность")

    published_urls = {str(url) for card in report.cards for url in card.candidate_urls}
    rank6_url, rank7_url, rank8_url = (f"https://example.org/dev/{n}" for n in (6, 7, 8))

    assert len(report.cards) == settings.target_cards
    assert rank7_url in published_urls, "склейка до среза обязана сохранить кандидата за старой границей"
    assert rank8_url not in published_urls, "кандидат ниже нового среза остаётся исключён, как и раньше"
    merged_card = next(card for card in report.cards if len(card.candidate_urls) == 2)
    assert {str(u) for u in merged_card.candidate_urls} == {rank6_url, rank7_url}
