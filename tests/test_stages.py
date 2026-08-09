"""Проверки чистых стадий пайплайна: склейка, отбор, группировка."""

from __future__ import annotations

from app.pipeline.stages import deduplicate, group_for_cards, select_top
from app.schemas import Candidate, ScoredCandidate, SourceType


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
