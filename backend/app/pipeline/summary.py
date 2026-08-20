"""Стадия 6 — сводка: сравнительная таблица вариантов и рекомендация.

Вынесено из `stages.py` (РЕШ-94) без изменения поведения — только перенос
тела функций и нужных импортов.
"""

from __future__ import annotations

import json

from app.llm import LLMRouter, Message
from app.pipeline.prompting import _GO, render_prompt
from app.schemas import Brief, ReportSummary, SolutionCard


# ── 6. Сводка ───────────────────────────────────────────────────────────────
async def summarize(router: LLMRouter, brief: Brief, cards: list[SolutionCard]) -> ReportSummary:
    """Сравнительная сводка и рекомендация по вариантам."""
    # Отдаём модели только то, что нужно для сравнения вариантов: полные
    # карточки с доказательствами раздули бы вход втрое без пользы для сводки.
    cards_json = json.dumps(
        [
            {
                "n": index,
                "title": card.title,
                "maturity_stage": card.maturity_stage.value,
                "essence": card.essence,
                "applicability": card.applicability,
                "risks": card.risks,
                "next_step": card.next_step,
                "confidence": card.confidence.value,
                "gov_support": [item.instrument for item in card.gov_support],
            }
            for index, card in enumerate(cards, start=1)
        ],
        ensure_ascii=False,
        indent=2,
    )
    return await router.complete_json(
        "summary",
        [
            Message(
                role="system",
                content=render_prompt(
                    "summary",
                    BRIEF_JSON=brief.model_dump_json(indent=2),
                    CARDS_JSON=cards_json,
                ),
            ),
            Message(role="user", content=_GO),
        ],
        ReportSummary,
        max_tokens=6000,
    )


def fallback_summary(cards: list[SolutionCard]) -> ReportSummary:
    """Запасная сводка, когда стадия summary отказала (Ф-7).

    Не вызывает модель — сравнительная таблица собирается прямо из уже
    провалидированных полей карточек, поэтому сама отказать не может. Отказ
    настоящей сводки не должен уничтожать уже собранные карточки: раньше
    именно это и происходило (прогон агропрома потерял пять готовых карточек
    из-за LLMError на summary). Факт отказа — честная оговорка в caveats, а
    не только запись в логе.
    """
    rows = [
        {
            "Вариант": card.title,
            "Стадия зрелости": card.maturity_stage.value,
            "Уверенность": card.confidence.value,
        }
        for card in cards
    ]
    if cards:
        recommendation = (
            "Автоматическая сводка не собралась. Сравните варианты по карточкам "
            "ниже самостоятельно: у каждой есть подтверждения, риски и следующий шаг."
        )
    else:
        recommendation = (
            "Автоматическая сводка не собралась, а карточек для сравнения тоже нет — "
            "результат поиска стоит проверить вручную."
        )
    return ReportSummary(
        comparison_rows=rows,
        recommendation=recommendation,
        caveats=["Автоматическая сводка не собралась: раздел «Рекомендация» построен без участия модели."],
    )
