"""Проверки чистых стадий пайплайна: склейка, отбор, группировка, честная
выдача карточек (Ф-7)."""

from __future__ import annotations

from app.pipeline.stages import deduplicate, group_for_cards, select_publishable_cards, select_top
from app.schemas import Candidate, Evidence, ScoredCandidate, SolutionCard, SourceType


def _card(title: str, *, evidence: bool = True) -> SolutionCard:
    """Минимально достаточная карточка для тестов отбора."""
    return SolutionCard(
        title=title,
        essence=f"Суть варианта «{title}».",
        applicability="Закрывает часть задачи из брифа.",
        next_step="Запросить у разработчика протокол испытаний.",
        evidence=[Evidence(claim="Факт из источника", url="https://example.org/a")] if evidence else [],
    )


def _placeholder_card() -> SolutionCard:
    """Каркас-заглушка из формата ошибок 1 `prompts/card_author.md`: группа
    кандидатов без источников."""
    return SolutionCard(
        title="не найдено",
        essence="не найдено",
        applicability="не найдено",
        next_step="не найдено",
        evidence=[],
    )


def test_deduplicate_merges_same_doi(candidates):
    """Дубли по DOI склеиваются: четыре кандидата превращаются в три варианта."""
    unique = deduplicate(candidates)
    assert len(unique) == 3


def test_deduplicate_prefers_showcase_over_open_source():
    """При равенстве ключа выигрывает витрина партнёров: там есть контакт разработчика."""
    common = {"title": "Технология регенерации", "year": 2025}
    open_source = Candidate(
        source_type=SourceType.PUBLICATION, url="https://example.org/a", provider="crossref", **common
    )
    partner = Candidate(source_type=SourceType.SHOWCASE, url="https://example.org/b", provider="showcase", **common)

    unique = deduplicate([open_source, partner])

    assert len(unique) == 1
    assert unique[0].provider == "showcase"


def test_select_top_limits_single_provider_dominance(candidates):
    """Один источник не должен занять всю выдачу, даже если его оценки выше."""
    scored = [
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title=f"Статья {i}",
                url=f"https://example.org/{i}",
                provider="crossref",
            ),
            relevance=10,
            maturity=8,
            accessibility=8,
            freshness=8,
            completeness=8,
        )
        for i in range(10)
    ]
    scored.append(
        ScoredCandidate(
            candidate=candidates[2],  # из витрины
            relevance=6,
            maturity=6,
            accessibility=6,
            freshness=6,
            completeness=6,
        )
    )

    picked = select_top(scored, limit=6)

    providers = {item.candidate.provider for item in picked}
    assert "showcase" in providers, "выдача не должна состоять из одного источника"
    assert sum(1 for i in picked if i.candidate.provider == "crossref") <= 3


def test_group_for_cards_merges_same_organisation():
    """Три публикации одной лаборатории — это один вариант решения, а не три карточки."""
    scored = [
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title=f"Работа {i}",
                url=f"https://example.org/{i}",
                org="Институт катализа",
                provider="crossref",
            ),
            relevance=8,
            maturity=6,
            accessibility=7,
            freshness=7,
            completeness=7,
        )
        for i in range(3)
    ]
    scored.append(
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title="Работа другой команды",
                url="https://example.org/other",
                org="Технический университет",
                provider="crossref",
            ),
            relevance=7,
            maturity=6,
            accessibility=6,
            freshness=6,
            completeness=6,
        )
    )

    groups = group_for_cards(scored, target=6)

    assert len(groups) == 2
    assert len(groups[0]) == 3


def test_group_for_cards_respects_target():
    """Больше карточек, чем заказано, пайплайн не отдаёт."""
    scored = [
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title=f"Работа {i}",
                url=f"https://example.org/{i}",
                org=f"Организация {i}",
                provider="crossref",
            ),
            relevance=8,
            maturity=6,
            accessibility=6,
            freshness=6,
            completeness=6,
        )
        for i in range(12)
    ]

    assert len(group_for_cards(scored, target=5)) == 5


def test_select_publishable_cards_drops_empty_evidence_and_placeholder():
    """Из шести карточек — две с пустым evidence и одна-пустышка «не найдено»
    во всех ключевых полях — в отчёт идут три честных, порядок сохраняется."""
    honest = [_card(f"Вариант {i}") for i in range(3)]
    no_evidence = [_card(f"Без источника {i}", evidence=False) for i in range(2)]
    cards = [honest[0], no_evidence[0], honest[1], _placeholder_card(), no_evidence[1], honest[2]]

    published = select_publishable_cards(cards)

    assert [c.title for c in published] == ["Вариант 0", "Вариант 1", "Вариант 2"]


def test_select_publishable_cards_caps_at_seven():
    """Девять честных карточек — в отчёте не больше семи (§2а SPEC, FR-9)."""
    cards = [_card(f"Вариант {i}") for i in range(9)]

    published = select_publishable_cards(cards)

    assert len(published) == 7
    assert [c.title for c in published] == [f"Вариант {i}" for i in range(7)]


def test_select_publishable_cards_keeps_all_when_under_limit():
    """Меньше семи честных карточек — нижней границы нет, отдаём сколько есть."""
    cards = [_card(f"Вариант {i}") for i in range(3)]

    published = select_publishable_cards(cards)

    assert len(published) == 3
