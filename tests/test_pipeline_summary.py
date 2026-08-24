"""Запасная сводка `fallback_summary` (П-4 аудита Э3).

Сравнительная таблица клиента печатала машинные значения перечислений
(`prototype`, `unknown`, `low`) вместо русских — правило языка CLAUDE.md
распространяется и на запасной путь без модели, а не только на путь через
`summarize`. Тесты без сети (инвариант №7): `fallback_summary` модель не
вызывает вовсе.
"""

from __future__ import annotations

from app.export.markdown import CONFIDENCE_RU, MATURITY_RU
from app.pipeline.summary import fallback_summary
from app.schemas import Confidence, Evidence, MaturityStage, SolutionCard


def _card(*, maturity: MaturityStage, confidence: Confidence, title: str) -> SolutionCard:
    return SolutionCard(
        title=title,
        essence="Суть решения для проверки локализации таблицы.",
        applicability="Закрывает проверяемую задачу.",
        next_step="Запросить дополнительные материалы.",
        evidence=[Evidence(claim="Факт из источника", url="https://doi.org/10.1/fallback-summary")],
        maturity_stage=maturity,
        confidence=confidence,
    )


def test_fallback_summary_prints_russian_maturity_and_confidence():
    """Таблица без модели — по-русски, а не машинными значениями перечислений."""
    cards = [
        _card(maturity=MaturityStage.PROTOTYPE, confidence=Confidence.LOW, title="Вариант А"),
        _card(maturity=MaturityStage.UNKNOWN, confidence=Confidence.HIGH, title="Вариант Б"),
    ]

    summary = fallback_summary(cards)

    row_a, row_b = summary.comparison_rows
    assert row_a["Стадия зрелости"] == MATURITY_RU[MaturityStage.PROTOTYPE]
    assert row_a["Уверенность"] == CONFIDENCE_RU[Confidence.LOW]
    assert row_b["Стадия зрелости"] == MATURITY_RU[MaturityStage.UNKNOWN]
    assert row_b["Уверенность"] == CONFIDENCE_RU[Confidence.HIGH]

    # Голых значений перечисления в таблице быть не должно (П-4 аудита Э3).
    rendered_values = {value for row in summary.comparison_rows for value in row.values()}
    assert "prototype" not in rendered_values
    assert "unknown" not in rendered_values
    assert "low" not in rendered_values


def test_fallback_summary_without_cards_has_no_rows():
    """Пустой список карточек — пустая таблица, без исключений."""
    summary = fallback_summary([])
    assert summary.comparison_rows == []
