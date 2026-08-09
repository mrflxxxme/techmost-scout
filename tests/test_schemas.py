"""Проверки контрактов данных: склейка дублей, веса скоринга, защита от пустых карточек."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    Candidate,
    Confidence,
    Evidence,
    MaturityStage,
    ScoredCandidate,
    SolutionCard,
    SourceType,
)


def test_dedup_key_prefers_identifier(candidates):
    """Один и тот же DOI из разных источников даёт один ключ склейки."""
    crossref, cyberleninka = candidates[0], candidates[1]
    assert crossref.dedup_key() == cyberleninka.dedup_key()


def test_dedup_key_falls_back_to_title():
    """Без идентификатора ключ строится по нормализованному названию:
    регистр, знаки и лишние пробелы не должны создавать ложных различий."""
    first = Candidate(
        source_type=SourceType.WEB,
        title="  Технология СУШКИ — опытный образец!  ",
        url="https://example.org/a",
        provider="web",
    )
    second = Candidate(
        source_type=SourceType.WEB,
        title="технология сушки опытный образец",
        url="https://example.org/b",
        provider="web",
    )
    assert first.dedup_key() == second.dedup_key()


def test_candidate_requires_url():
    """Кандидат без ссылки не должен существовать: на нём нельзя построить доказательство."""
    with pytest.raises(ValidationError):
        Candidate(source_type=SourceType.WEB, title="Без ссылки", url="не ссылка", provider="web")


def test_absurd_year_is_dropped():
    """Модели иногда выдают год 20255 — такое значение лучше потерять, чем показать клиенту."""
    candidate = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Статья",
        url="https://example.org/a",
        year=20255,
        provider="crossref",
    )
    assert candidate.year is None


def test_score_weights_favor_relevance_and_access(candidates):
    """Кандидат, который по задаче и с доступным разработчиком, обгоняет просто свежего."""
    relevant = ScoredCandidate(
        candidate=candidates[0], relevance=9, maturity=5, accessibility=9, freshness=5, completeness=6
    )
    merely_fresh = ScoredCandidate(
        candidate=candidates[3], relevance=5, maturity=5, accessibility=3, freshness=10, completeness=6
    )
    assert relevant.total > merely_fresh.total


def test_card_without_evidence_is_flagged():
    """Карточка без подтверждений обязана уметь о себе сообщить."""
    card = SolutionCard(
        title="Решение",
        essence="Суть",
        maturity_stage=MaturityStage.LAB,
        applicability="Закрывает задачу",
        next_step="Запросить протокол испытаний",
    )
    assert card.has_unsourced_claims() is True

    card.evidence.append(Evidence(claim="Срок службы вырос на 40%", url="https://doi.org/10.1234/x"))
    assert card.has_unsourced_claims() is False
    assert card.confidence is Confidence.MEDIUM
