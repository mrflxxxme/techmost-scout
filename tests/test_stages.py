"""Проверки чистых стадий пайплайна: склейка, отбор, отсечка по предметному
соответствию, группировка, честная выдача карточек (Ф-7)."""

from __future__ import annotations

import logging

from app.config import Settings
from app.pipeline import stages
from app.pipeline.stages import (
    NEUTRAL_SCORE_NOTE,
    deduplicate,
    group_for_cards,
    select_publishable_cards,
    select_top,
)
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


def _scored(number: int, *, relevance: int, provider: str = "crossref", notes: str = "") -> ScoredCandidate:
    """Оценённый кандидат с заданной релевантностью; прочие оценки одинаковы,
    чтобы порядок отбора зависел только от `relevance`."""
    return ScoredCandidate(
        candidate=Candidate(
            source_type=SourceType.PUBLICATION,
            title=f"Кандидат {number}",
            url=f"https://example.org/{number}",
            provider=provider,
        ),
        relevance=relevance,
        maturity=8,
        accessibility=8,
        freshness=8,
        completeness=8,
        notes=notes,
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


def test_relevance_min_default_is_zero_cutoff_disabled():
    """Порог по умолчанию — 0: отсечка выключена, пока значение не подобрано на
    прогоне (Т-10). Догадка в коде хуже отсутствия отсечки."""
    field = Settings.model_fields["relevance_min"]

    assert field.default == 0


def test_select_top_drops_candidate_below_relevance_min():
    """Кандидат с relevance ниже порога в отбор не попадает (РЕШ-48)."""
    scored = [_scored(0, relevance=9), _scored(1, relevance=3)]

    picked = select_top(scored, limit=6, relevance_min=6)

    assert [item.candidate.title for item in picked] == ["Кандидат 0"]


def test_select_top_keeps_neutral_score_at_same_threshold():
    """Нейтральная оценка отсечкой не режется никогда: её проставил код после
    отказа батча скоринга, и отсечь её — значит выдать отказ стадии за «все
    нерелевантны». Порог тот же, что и в тесте выше."""
    scored = [
        _scored(0, relevance=9),
        _scored(1, relevance=3),
        _scored(2, relevance=5, notes=NEUTRAL_SCORE_NOTE),
    ]

    picked = select_top(scored, limit=6, relevance_min=6)

    titles = [item.candidate.title for item in picked]
    assert "Кандидат 2" in titles, "кандидат с нейтральной оценкой отсечён"
    assert "Кандидат 1" not in titles


def test_select_top_with_zero_threshold_keeps_previous_behaviour():
    """При relevance_min=0 состав отбора совпадает с прежним поведением: отсечка
    выключена, отбирают только сумма оценок и лимит на один источник."""
    scored = [_scored(number, relevance=number) for number in range(8)]

    with_cutoff_off = select_top(scored, limit=4, relevance_min=0)

    # Прежнее поведение: сортировка по total и потолок на один источник.
    ordered = sorted(scored, key=lambda s: s.total, reverse=True)
    assert [item.candidate.title for item in with_cutoff_off] == [item.candidate.title for item in ordered[:2]]


def test_select_top_reads_threshold_from_settings(monkeypatch):
    """Без явного аргумента порог берётся из настроек — иначе `RELEVANCE_MIN`
    остался бы настройкой, которую никто не читает."""
    monkeypatch.setattr(stages, "get_settings", lambda: Settings(relevance_min=7))
    scored = [_scored(0, relevance=9), _scored(1, relevance=3)]

    picked = select_top(scored, limit=6)

    assert [item.candidate.title for item in picked] == ["Кандидат 0"]


def test_select_top_logs_calibration_even_when_nothing_is_cut(caplog):
    """Строка калибровки печатается всегда, в том числе при пороге 0 и нулевой
    отсечке: это единственный источник данных для Т-10 (сохранённых оценок нет),
    и ноль обязан быть отличим от «отсечка не сработала»."""
    scored = [_scored(number, relevance=relevance) for number, relevance in enumerate([3, 5, 5, 8, 10])]

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top(scored, limit=6, relevance_min=0)

    line = next((r.getMessage() for r in caplog.records if "калибровка relevance" in r.getMessage()), None)
    assert line is not None, "строки калибровки нет в журнале"
    assert "кандидатов 5" in line
    assert "3/5/10" in line, f"нет распределения min/медиана/max: {line}"
    assert "порог 0" in line
    assert "отсечено 0" in line
    # Колонка «отсеклось бы»: при 4 уходит один кандидат, при 6 — три, при 7 — три.
    assert "при 4: 1" in line and "при 5: 1" in line and "при 6: 3" in line and "при 7: 3" in line, line


def test_calibration_line_counts_neutral_scores_separately(caplog):
    """Нейтральные оценки в журнале считаются отдельно и не попадают в «отсеклось
    бы»: иначе калибровка Т-10 приняла бы отказ батча за нерелевантных кандидатов."""
    scored = [_scored(0, relevance=9), _scored(1, relevance=5, notes=NEUTRAL_SCORE_NOTE)]

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top(scored, limit=6, relevance_min=0)

    line = next(r.getMessage() for r in caplog.records if "калибровка relevance" in r.getMessage())
    assert "нейтральных оценок 1" in line
    assert "при 6: 0" in line, f"нейтральная оценка попала в «отсеклось бы»: {line}"


def test_calibration_line_printed_on_empty_input(caplog):
    """Кандидатов нет — строка всё равно печатается: пустой отбор обязан быть
    отличим от неработающей стадии."""
    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top([], limit=6, relevance_min=0)

    assert any("калибровка relevance" in r.getMessage() for r in caplog.records)


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
