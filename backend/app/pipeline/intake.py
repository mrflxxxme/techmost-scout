"""Стадия 1 — интейк: из свободного текста задачи в уточняющие вопросы и бриф.

Вынесено из `stages.py` (РЕШ-94) без изменения поведения — только перенос
тела функций и нужных импортов.
"""

from __future__ import annotations

from app.llm import LLMRouter, Message
from app.pipeline.prompting import _GO, neutralize_placeholders, render_prompt
from app.schemas import Brief, IntakeResult

#: Предел длины `task_summary` запасного брифа — короткое описание, а не
#: письмо целиком; отчёту хватает первых сотен знаков для сверки.
_FALLBACK_SUMMARY_LIMIT = 500


def brief_from_raw_text(raw_text: str, *, industry_hint: str = "не указана") -> Brief:
    """Собрать бриф без вызова модели — запасной путь при отказе интейка.

    Ничего не домысливает (инвариант №1): отрасль берётся из подсказки
    оператора либо остаётся «не указана», `task_summary` — обрезанный сырой
    текст задачи, остальные поля брифа пустые. Единственная правка текста —
    обезвреживание меток `{{...}}`, см. `neutralize_placeholders`.
    """
    summary = neutralize_placeholders(raw_text.strip())
    if len(summary) > _FALLBACK_SUMMARY_LIMIT:
        summary = summary[:_FALLBACK_SUMMARY_LIMIT].rstrip() + "…"
    return Brief(industry=neutralize_placeholders(industry_hint), task_summary=summary or "не указано")


# ── 1. Интейк ───────────────────────────────────────────────────────────────
async def run_intake(router: LLMRouter, raw_text: str, *, industry_hint: str = "не указана") -> IntakeResult:
    """Из свободного текста — уточняющие вопросы и черновик брифа."""
    return await router.complete_json(
        "intake",
        [
            Message(
                role="system",
                content=render_prompt("intake", RAW_TASK_TEXT=raw_text, INDUSTRY_HINT=industry_hint),
            ),
            Message(role="user", content=_GO),
        ],
        IntakeResult,
        # Т-13: стадия жила на умолчании роутера в 4096 — а это ровно тот
        # скупой лимит, на котором рассуждающая модель отдаёт пустой `content`.
        # Один заполненный `IntakeResult` (пять вопросов с обоснованиями плюс
        # полностью набитый `Brief` с ответами владельца) — 8 148 знаков,
        # ≈4 070 токенов одного только ответа: на рассуждение не остаётся
        # ничего. 8000 — ответ плюс сопоставимый запас на рассуждение.
        max_tokens=8000,
    )


async def refine_brief(
    router: LLMRouter, raw_text: str, answers: dict[str, str], *, industry_hint: str = "не указана"
) -> Brief:
    """Собрать финальный бриф с учётом ответов на уточняющие вопросы."""
    answers_text = "\n".join(f"— {q}\n  Ответ: {a or 'не знаю'}" for q, a in answers.items())
    enriched = f"{raw_text}\n\nОтветы на уточняющие вопросы:\n{answers_text}"
    result = await router.complete_json(
        "intake",
        [
            Message(
                role="system",
                content=render_prompt("intake", RAW_TASK_TEXT=enriched, INDUSTRY_HINT=industry_hint),
            ),
            Message(
                role="user",
                content=(
                    "Уточнения уже получены — вопросов больше не задавай: верни questions "
                    "пустым списком и полностью заполненный draft_brief. Только JSON."
                ),
            ),
        ],
        IntakeResult,
        # Тот же расчёт, что у `run_intake`: ответ здесь ещё длиннее — вопросов
        # нет, зато `draft_brief` заполнен целиком.
        max_tokens=8000,
    )
    return result.draft_brief
