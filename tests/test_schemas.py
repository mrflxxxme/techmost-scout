"""Проверки контрактов данных: склейка дублей, веса скоринга, защита от пустых карточек."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    Candidate,
    Confidence,
    Evidence,
    MaturityStage,
    RequirementCheck,
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


# ── Ф-1: MaturityStage.UNKNOWN ────────────────────────────────────────────


def test_maturity_stage_unknown_member_exists():
    """Перечисление получает новый член: карточка имеет право не заявлять
    зрелость, если ни один признак не подтверждён источниками."""
    assert MaturityStage("unknown") is MaturityStage.UNKNOWN


def test_solution_card_without_maturity_defaults_to_unknown():
    """Карточка, собранная без maturity_stage (в т.ч. карточка-пустышка «не
    найдено»), не обязана назначать зрелость структурно — раньше это было
    невозможно без явного значения (инвариант №1)."""
    card = SolutionCard(
        title="Решение",
        essence="Суть",
        applicability="Закрывает задачу",
        next_step="Запросить протокол испытаний",
    )
    assert card.maturity_stage is MaturityStage.UNKNOWN


def test_maturity_stage_readers_accept_every_member_including_unknown():
    """showcase.py читает maturity_stage как обычный текст (в snippet, без
    конструирования перечисления), а cli.py конструирует MaturityStage() из
    флага --maturity и из значений CSV-загрузчика витрины. Ни одно из этих
    мест не должно падать ни на одном члене перечисления, включая новый
    unknown — критерий 5 задачи Ф-1."""
    values_seen_by_cli_and_showcase = ("lab", "prototype", "pilot", "series", "unknown")
    for raw in values_seen_by_cli_and_showcase:
        assert MaturityStage(raw).value == raw


# ── Ф-1: RequirementCheck и requirement_checks ────────────────────────────


def test_requirement_check_rejects_status_outside_pair():
    """Статус построчной сверки — только confirmed/not_confirmed, без
    домысливания третьего значения."""
    with pytest.raises(ValidationError):
        RequirementCheck(requirement="Работа при 340 °C", status="maybe")


def test_solution_card_requirement_checks_and_unsourced_claims_default_empty():
    """Новые поля не ломают сборку карточки без них и по умолчанию пусты."""
    card = SolutionCard(
        title="Решение",
        essence="Суть",
        applicability="Закрывает задачу",
        next_step="Запросить протокол испытаний",
    )
    assert card.requirement_checks == []
    assert card.unsourced_claims == []
    assert card.pdf_urls == []

    card.requirement_checks.append(
        RequirementCheck(requirement="Работа при 340 °C", status="confirmed", evidence_url="https://doi.org/10.1234/x")
    )
    assert card.requirement_checks[0].status == "confirmed"


# ── Ф-1: нормализация DOI в dedup_key ─────────────────────────────────────


def test_dedup_key_normalizes_doi_version_suffix():
    """Препринт и финальная публикация той же статьи TechRxiv (различаются
    только хвостом версии .vN) обязаны склеиваться в одну карточку."""
    versioned = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Черновик",
        url="https://www.techrxiv.org/a",
        raw_id="10.36227/techrxiv.23939970.v1",
        provider="crossref",
    )
    unversioned = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Финальная версия",
        url="https://www.techrxiv.org/b",
        raw_id="10.36227/techrxiv.23939970",
        provider="crossref",
    )
    assert versioned.dedup_key() == unversioned.dedup_key()


def test_dedup_key_normalizes_doi_case_and_url_prefix():
    """Один и тот же DOI как ссылка doi.org (в верхнем регистре) и как голый
    идентификатор обязаны давать один ключ."""
    as_url = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Статья",
        url="https://doi.org/10.1016/J.FUEL.2024.1",
        raw_id="https://doi.org/10.1016/J.FUEL.2024.1",
        provider="crossref",
    )
    as_bare_id = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Статья",
        url="https://example.org/fuel",
        raw_id="10.1016/j.fuel.2024.1",
        provider="cyberleninka",
    )
    assert as_url.dedup_key() == as_bare_id.dedup_key()


def test_dedup_key_does_not_collapse_different_dois():
    """Нормализация не должна склеивать два разных DOI: проверка на
    «нормализовали слишком много» (критерий 2 задачи Ф-1)."""
    first = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Статья A",
        url="https://doi.org/10.1016/j.fuel.2024.1",
        raw_id="10.1016/j.fuel.2024.1",
        provider="crossref",
    )
    second = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Статья B",
        url="https://doi.org/10.1016/j.fuel.2024.2",
        raw_id="10.1016/j.fuel.2024.2",
        provider="crossref",
    )
    assert first.dedup_key() != second.dedup_key()


def test_dedup_key_version_suffix_pattern_is_strict():
    """Хвост версии — только `.v<цифры>` в конце ключа. Идентификатор, который
    просто содержит `.v` не на конце или без цифр, не должен обрезаться."""
    candidate = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Патент",
        url="https://example.org/patent",
        raw_id="RU2799999.version",
        provider="rospatent",
    )
    assert candidate.dedup_key() == "ru2799999.version"


# ── Ф-1: Candidate.pdf_url ─────────────────────────────────────────────────


def test_candidate_pdf_url_defaults_to_none_and_accepts_value():
    without_pdf = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Статья",
        url="https://example.org/a",
        provider="semanticscholar",
    )
    assert without_pdf.pdf_url is None

    with_pdf = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Статья",
        url="https://example.org/a",
        pdf_url="https://example.org/a.pdf",
        provider="semanticscholar",
    )
    assert str(with_pdf.pdf_url) == "https://example.org/a.pdf"
