"""Слой аналогов, чистые части (Э6-2а): отбор, сериализатор, карточка, план поиска.

Сети нет: модель — `FakeProvider` из `conftest.py` (инвариант №7). Сквозной прогон
оркестратора — в `test_analog_flow.py`.
"""

from __future__ import annotations

import json

import pytest

from app.countries import CountryStatus, country_status
from app.llm import LLMRouter
from app.pipeline import analogs, planning, stages
from app.pipeline.constraints_guard import retry_constraints_if_missing
from app.pipeline.pipeline_selection_flow import ANALOG_CARDS_TARGET, split_analog_candidates
from app.schemas import (
    AnalogCard,
    Brief,
    Candidate,
    Evidence,
    ScoredCandidate,
    SearchPlan,
    SearchQuery,
    SourceType,
)


def _candidate(number: int, *, country: str | None = None, source_type: SourceType = SourceType.ANALOG) -> Candidate:
    return Candidate(
        source_type=source_type,
        title=f"Cold chain monitoring SaaS №{number}",
        url=f"https://example.com/product-{number}",
        org=f"Company {number}",
        year=2025,
        snippet="A cloud service that tracks cargo temperature.",
        provider="yandex_search",
        country=country,
    )


def _scored(candidate: Candidate, relevance: int = 7) -> ScoredCandidate:
    return ScoredCandidate(
        candidate=candidate, relevance=relevance, maturity=5, accessibility=5, freshness=5, completeness=5
    )


def _card(
    *,
    country: str = "US",
    evidence_url: str = "https://example.com/product-1",
    candidate_urls: list[str] | None = None,
    with_evidence: bool = True,
) -> AnalogCard:
    return AnalogCard(
        title="Мониторинг холодовой цепи",
        essence="Облачный сервис следит за температурой груза.",
        country=country,
        country_evidence_url=evidence_url,
        business_model="Подписка.",
        transferability_ru="не установлено",
        what_to_do_in_ru="Проверить поставки в РФ у разработчика.",
        evidence=[Evidence(claim="Сервис следит за температурой", url=evidence_url)] if with_evidence else [],
        candidate_urls=candidate_urls if candidate_urls is not None else [evidence_url],
    )


def _model_answer(*, country: str = "US", evidence_url: str = "https://example.com/product-1") -> str:
    """Ответ модели по схеме `AnalogCard` — без `candidate_urls`, их ставит код."""
    return json.dumps(
        {
            "title": "Мониторинг холодовой цепи",
            "essence": "Облачный сервис следит за температурой груза.",
            "country": country,
            "country_evidence_url": evidence_url,
            "business_model": "Подписка.",
            "traction": "",
            "transferability_ru": "не установлено",
            "what_to_do_in_ru": "Проверить поставки в РФ у разработчика.",
            "evidence": [{"claim": "Сервис следит за температурой", "url": evidence_url}],
        },
        ensure_ascii=False,
    )


def _router(settings, fake_provider_factory, *responses: str):
    provider = fake_provider_factory("deepseek", list(responses))
    return LLMRouter(settings, providers={"deepseek": provider}), provider


# ── Деление на аналоги и прочих ──────────────────────────────────────────────
def test_split_analog_candidates_делит_по_типу_источника():
    analog = _scored(_candidate(1))
    paper = _scored(_candidate(2, source_type=SourceType.PUBLICATION))
    contest = _scored(_candidate(3, source_type=SourceType.CONTEST))

    rest, analog_part = split_analog_candidates([analog, paper, contest])

    assert analog_part == [analog]
    assert rest == [paper, contest]


def test_analog_не_идёт_в_покрытие_типов_источников():
    papers = [_candidate(1, source_type=SourceType.PUBLICATION), _candidate(2, source_type=SourceType.PATENT)]

    assert stages.count_source_type_coverage([*papers, _candidate(3)]) == stages.count_source_type_coverage(papers)


# ── Фильтр по стране до модели ───────────────────────────────────────────────
def test_prefilter_снимает_тайвань_в_режиме_gov():
    taiwan, germany = _scored(_candidate(1, country="TW")), _scored(_candidate(2, country="DE"))

    assert analogs.prefilter_analog_candidates([taiwan, germany], "gov") == [germany]


def test_prefilter_в_режиме_business_пропускает_всех():
    items = [_scored(_candidate(1, country="TW")), _scored(_candidate(2, country="DE"))]

    assert analogs.prefilter_analog_candidates(items, "business") == items


def test_prefilter_не_трогает_кандидата_без_страны():
    """Страны нет — снимать нечего: её назовёт модель, а проверит отбор по готовой карточке."""
    item = _scored(_candidate(1, country=None))

    assert analogs.prefilter_analog_candidates([item], "gov") == [item]


# ── Сериализатор со страной ──────────────────────────────────────────────────
def test_analog_candidates_to_json_несёт_страну_а_общий_сериализатор_нет():
    group = [_scored(_candidate(1, country="US")), _scored(_candidate(2, country=None))]

    payload = json.loads(analogs.analog_candidates_to_json(group))
    common = json.loads(stages.candidates_to_json(group))

    assert [item["country"] for item in payload] == ["US", None]
    assert all("country" not in item for item in common), "общий сериализатор менять нельзя: он уходит в промпты «Науки»"


# ── Сборка карточки ──────────────────────────────────────────────────────────
async def test_author_analog_card_ставит_ссылки_группы_кодом(settings, brief, fake_provider_factory):
    group = [_scored(_candidate(1, country="US")), _scored(_candidate(2, country="US"))]
    router, provider = _router(settings, fake_provider_factory, _model_answer())

    card = await analogs.author_analog_card(router, brief, group)

    assert card is not None
    assert [str(url) for url in card.candidate_urls] == [str(item.candidate.url) for item in group]
    system = provider.calls[0][0].content
    assert "{{" not in system, "метка промпта осталась незаполненной"
    assert "https://example.com/product-2" in system, "кандидаты группы в промпт не попали"
    assert '"country": "US"' in system, "страна кандидата в промпт не попала"


async def test_страна_модели_расходится_со_структурной_карточка_не_публикуется(settings, brief, fake_provider_factory):
    group = [_scored(_candidate(1, country="TW"))]
    router, _ = _router(settings, fake_provider_factory, _model_answer(country="US"))

    assert await analogs.author_analog_card(router, brief, group) is None


async def test_у_кандидатов_группы_разные_страны_карточка_не_публикуется(settings, brief, fake_provider_factory):
    group = [_scored(_candidate(1, country="US")), _scored(_candidate(2, country="DE"))]
    router, _ = _router(settings, fake_provider_factory, _model_answer(country="US"))

    assert await analogs.author_analog_card(router, brief, group) is None


async def test_структурная_страна_совпала_с_названной_моделью_карточка_собрана(settings, brief, fake_provider_factory):
    group = [_scored(_candidate(1, country="DE"))]
    router, _ = _router(settings, fake_provider_factory, _model_answer(country="DE"))

    card = await analogs.author_analog_card(router, brief, group)

    assert card is not None
    assert card.country == "DE"


async def test_страна_не_установлена_и_структурной_нет_карточка_не_публикуется(settings, brief, fake_provider_factory):
    group = [_scored(_candidate(1, country=None))]
    router, _ = _router(settings, fake_provider_factory, _model_answer(country=analogs.UNKNOWN_COUNTRY))

    assert await analogs.author_analog_card(router, brief, group) is None


async def test_страна_названа_моделью_без_структурной_принимается_к_отбору(settings, brief, fake_provider_factory):
    """Кандидат без страны: страну называет модель, а допуск решает `select_publishable_analogs`."""
    group = [_scored(_candidate(1, country=None))]
    router, _ = _router(settings, fake_provider_factory, _model_answer(country="TW"))

    card = await analogs.author_analog_card(router, brief, group)

    assert card is not None
    assert card.country == "TW"


# ── Отбор публикуемых ────────────────────────────────────────────────────────
def test_select_без_evidence_не_публикуется():
    assert analogs.select_publishable_analogs([_card(with_evidence=False)], "business", 3) == []


def test_select_ссылка_страны_не_из_источников_группы_не_публикуется():
    card = _card(evidence_url="https://invented.example/", candidate_urls=["https://example.com/product-1"])

    assert analogs.select_publishable_analogs([card], "business", 3) == []


def test_select_исключённая_страна_в_gov_не_публикуется_а_в_business_публикуется():
    card = _card(country="TW")

    assert analogs.select_publishable_analogs([card], "gov", 3) == []
    assert analogs.select_publishable_analogs([card], "business", 3) == [card]


def test_select_сноска_это_допуск():
    """`footnote` не `excluded`: карточка идёт дальше, куда её печатать — решают выгрузки."""
    card = _card(country="DE")
    assert country_status("DE", "gov") is CountryStatus.FOOTNOTE

    assert analogs.select_publishable_analogs([card], "gov", 3) == [card]


def test_select_неустановленная_страна_не_публикуется():
    assert analogs.select_publishable_analogs([_card(country=analogs.UNKNOWN_COUNTRY)], "business", 3) == []


def test_select_режет_по_пределу_и_держит_порядок():
    cards = [_card(), _card(), _card(), _card()]

    assert analogs.select_publishable_analogs(cards, "business", ANALOG_CARDS_TARGET) == cards[:ANALOG_CARDS_TARGET]


# ── План поиска ──────────────────────────────────────────────────────────────
def _q(text: str, source_type: SourceType, lang: str = "en") -> SearchQuery:
    return SearchQuery(text=text, source_type=source_type, lang=lang, rationale="тест")


def _plan_with_analogs(count: int = 2) -> SearchPlan:
    base = [
        _q("катализатор", SourceType.PUBLICATION, "ru"),
        _q("catalyst", SourceType.PUBLICATION),
        _q("лаборатория", SourceType.WEB, "ru"),
        _q("lab", SourceType.WEB),
    ]
    analog = [_q(f"cold chain saas {n}", SourceType.ANALOG) for n in range(count)]
    return SearchPlan(queries=[*base, *analog])


def _types(plan: SearchPlan) -> list[SourceType]:
    return [query.source_type for query in plan.queries]


def test_research_снимает_analog_кодом(brief):
    assert brief.solution_kind == "research"

    plan = planning.normalize_analog_queries(_plan_with_analogs(2), brief)

    assert SourceType.ANALOG not in _types(plan)
    assert len(plan.queries) == 4


@pytest.mark.parametrize("kind", ["product", "digital", "business_model"])
def test_рынок_оставляет_две_формулировки_analog(brief, kind):
    market = brief.model_copy(update={"solution_kind": kind})

    plan = planning.normalize_analog_queries(_plan_with_analogs(4), market)

    assert _types(plan).count(SourceType.ANALOG) == planning.ANALOG_QUERIES == 2


def test_нормализация_плана_не_вытесняет_analog_конкурсными(brief):
    """Потолок схемы 12: девять формулировок модели + две `analog` + две конкурсные не помещаются,
    и вытесняться должны формулировки основной очереди, а не аналоги в хвосте."""
    market = brief.model_copy(update={"solution_kind": "product"})
    base = [_q(f"запрос {n}", SourceType.PUBLICATION, "ru") for n in range(9)]
    plan = SearchPlan(queries=[*base, *[_q(f"cold chain {n}", SourceType.ANALOG) for n in range(2)]])

    result = planning.normalize_plan_queries(plan, market)

    assert len(result.queries) == 12
    assert _types(result).count(SourceType.ANALOG) == 2
    assert _types(result).count(SourceType.CONTEST) == 2


def test_second_pass_не_несёт_analog():
    plan = planning.strip_analog_queries(_plan_with_analogs(2))

    assert SourceType.ANALOG not in _types(plan)


async def test_plan_search_отдаёт_вид_решения_в_промпт_и_снимает_analog_при_research(
    settings, brief, fake_provider_factory
):
    answer = _plan_with_analogs(2).model_dump_json()
    router, provider = _router(settings, fake_provider_factory, answer)

    plan = await stages.plan_search(router, brief)

    assert SourceType.ANALOG not in _types(plan)
    system = provider.calls[0][0].content
    assert "{{" not in system
    assert "```\nresearch\n```" in system


async def test_plan_search_при_рынке_сохраняет_analog_и_называет_вид(settings, brief, fake_provider_factory):
    market = brief.model_copy(update={"solution_kind": "digital"})
    router, provider = _router(settings, fake_provider_factory, _plan_with_analogs(2).model_dump_json())

    plan = await stages.plan_search(router, market)

    assert _types(plan).count(SourceType.ANALOG) == 2
    assert "```\ndigital\n```" in provider.calls[0][0].content


# ── Метка вида решения в guard интейка (совет ревью Э6-1) ────────────────────
async def test_guard_ограничений_передаёт_вид_решения_при_не_research(settings, brief, fake_provider_factory):
    market = brief.model_copy(update={"solution_kind": "product", "constraints": []})
    answer = json.dumps(
        {"questions": [], "draft_brief": {"industry": "логистика", "task_summary": "Нужен мониторинг", "constraints": ["не выше 8 °C"]}},
        ensure_ascii=False,
    )
    router, provider = _router(settings, fake_provider_factory, answer)

    result, unresolved = await retry_constraints_if_missing(
        router, market, "Температура в кузове не выше 8 °C, давление 5 МПа", industry_hint="логистика", stage_log=[]
    )

    assert unresolved is False
    assert result.constraints == ["не выше 8 °C"]
    system = provider.calls[0][0].content
    assert "```\nproduct\n```" in system, "вид решения не дошёл до промпта интейка"
    assert isinstance(result, Brief)
