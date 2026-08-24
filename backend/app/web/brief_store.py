"""Хранилище брифа: единственное место, где веб пишет и читает `schemas.Brief`
(задача И-9, РЕШ-156).

Два экрана волны 2 (уточняющие вопросы и подтверждение брифа, И-10) и хаб
запроса (И-11) обращаются к брифу только через три функции этого модуля —
иначе маршруты собирали бы строку `briefs` порознь и разошлись бы форматом.
Таблица `briefs` до этапа 3 не писалась ни разу: ORM-модель существовала, но в
сессию не попадала (РЕШ-156). Здесь она оживает на веб-пути — рядом с
прогонной записью `app.pipeline.persistence_write` (задача И-5), которая
пишет ту же таблицу, когда бриф уже собран пайплайном. Обе стороны следуют
одному контракту колонок `models.Brief` и делают upsert по `request_id`
(`unique`), поэтому конфликтов при повторной записи нет: кто бы ни писал
вторым, он обновляет ту же строку, а не вставляет вторую.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Brief as BriefRow
from app.models import utcnow
from app.pipeline.stages import neutralize_placeholders
from app.schemas import Brief, MaturityStage, OwnerAnswer


def _clean(text: str) -> str:
    """Обезвредить метки `{{...}}` в тексте, пришедшем от оператора (инвариант
    №4): без этого метка вида `{{ЧТО-ТО}}`, случайно введённая в поле формы,
    сломала бы подстановку в промпт следующей стадии пайплайна."""
    return neutralize_placeholders(text.strip())


def _clean_optional(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = _clean(text)
    return cleaned or None


def _clean_list(value: Any) -> list[str]:
    """Список строк из поля формы.

    Текстовое поле-`textarea` приходит одной строкой с переносами, уже
    собранный список — списком: маршрут волны 2 сам решает, каким полем
    прислать ограничения и ключевые слова, эта функция принимает оба вида.
    Пустые строки (лишние пустые строки textarea) отбрасываются.
    """
    if not value:
        return []
    items = value.splitlines() if isinstance(value, str) else list(value)
    return [_clean(str(item)) for item in items if str(item).strip()]


def _owner_answers_from(value: Any) -> list[OwnerAnswer]:
    """Ответы оператора из поля формы: список пар вопрос/ответ (объекты
    `OwnerAnswer` или словари с теми же ключами — оба вида допустимы)."""
    if not value:
        return []
    result: list[OwnerAnswer] = []
    for item in value:
        if isinstance(item, OwnerAnswer):
            question, answer = item.question, item.answer
        else:
            question, answer = item["question"], item["answer"]
        result.append(OwnerAnswer(question=_clean(str(question)), answer=_clean(str(answer))))
    return result


def brief_from_form(fields: Mapping[str, Any]) -> Brief:
    """Собрать `schemas.Brief` из полей формы подтверждения брифа.

    Поля формы совпадают с полями схемы (РЕШ-143 п. 4): `industry`,
    `task_summary`, `constraints`, `owner_answers`, `target_maturity`,
    `geography`, `budget_hint`, `timeline_hint`, `keywords_ru`, `keywords_en` —
    маршрут правит ровно то, что уходит в базу, без перекладывания имён.

    Единственное исключение — `target_trl`: у схемы `Brief` поля TRL нет и не
    появится (РЕШ-185, схема и боевая база не меняются); значение из формы
    дописывается отдельной строкой в `constraints` вида «целевой TRL 6» —
    так требование доезжает до построчной сверки `requirement_checks`, не
    заводя новой колонки. Пустое значение TRL строку не добавляет.

    Весь пришедший от оператора текст проходит `neutralize_placeholders`
    (инвариант №4) — метка `{{...}}` в любом поле формы обезвреживается
    раньше, чем бриф попадёт в промпт следующей стадии.
    """
    constraints = _clean_list(fields.get("constraints"))
    target_trl = fields.get("target_trl")
    if target_trl not in (None, ""):
        # `target_trl` — то же поле формы, что и остальные: значение проходит
        # `_clean` наравне с ними, прежде чем уйти в constraints строкой
        # (иначе метка `{{...}}` в этом поле осталась бы необезвреженной —
        # ревью цикла 1, инвариант №4).
        constraints.append(f"целевой TRL {_clean(str(target_trl))}")

    target_maturity_raw = fields.get("target_maturity") or None
    target_maturity = MaturityStage(target_maturity_raw) if target_maturity_raw else None

    geography_raw = fields.get("geography")
    geography = _clean(str(geography_raw)) if geography_raw else "РФ"

    return Brief(
        industry=_clean(str(fields.get("industry", ""))),
        task_summary=_clean(str(fields.get("task_summary", ""))),
        constraints=constraints,
        owner_answers=_owner_answers_from(fields.get("owner_answers")),
        target_maturity=target_maturity,
        geography=geography,
        budget_hint=_clean_optional(fields.get("budget_hint")),
        timeline_hint=_clean_optional(fields.get("timeline_hint")),
        keywords_ru=_clean_list(fields.get("keywords_ru")),
        keywords_en=_clean_list(fields.get("keywords_en")),
    )


async def save_brief(session: AsyncSession, request_id: int, brief: Brief, *, confirmed: bool) -> BriefRow:
    """Upsert строки `briefs` по `request_id` (колонка `unique`).

    Строка уже существует, если веб сохранял бриф раньше (черновик после
    интейка) или пайплайн дописал его при прогоне (`persistence_write`,
    задача И-5) — в обоих случаях функция обновляет её на месте, а не
    вставляет вторую: второй `INSERT` на тот же `request_id` уронил бы
    транзакцию ограничением `unique`.

    `confirmed=True` проставляет `confirmed_at` текущим временем (в том числе
    повторно, если оператор подтвердил правку после подтверждения). При
    `confirmed=False` колонка не трогается: у новой строки она и так `NULL`
    (умолчание колонки), а сброс уже стоящей отметки этой функции не нужен —
    подтверждённый бриф остаётся подтверждённым, пока не появится маршрут,
    который решит иначе.
    """
    found = await session.execute(select(BriefRow).where(BriefRow.request_id == request_id))
    row = found.scalars().one_or_none()
    if row is None:
        row = BriefRow(request_id=request_id)
        session.add(row)

    row.industry = brief.industry
    row.task_summary = brief.task_summary
    row.constraints = list(brief.constraints)
    row.owner_answers = [item.model_dump(mode="json") for item in brief.owner_answers]
    row.target_maturity = brief.target_maturity.value if brief.target_maturity is not None else None
    row.geography = brief.geography
    row.budget_hint = brief.budget_hint
    row.timeline_hint = brief.timeline_hint
    row.keywords_ru = list(brief.keywords_ru)
    row.keywords_en = list(brief.keywords_en)
    if confirmed:
        row.confirmed_at = utcnow()

    await session.flush()
    return row


async def load_brief(session: AsyncSession, request_id: int) -> Brief | None:
    """Обратное преобразование: строка `briefs` → `schemas.Brief`.

    `None`, если у запроса брифа ещё нет — веб не дошёл до сохранения
    (статус `intake`) либо запроса с таким номером не существует.
    """
    found = await session.execute(select(BriefRow).where(BriefRow.request_id == request_id))
    row = found.scalars().one_or_none()
    if row is None:
        return None

    return Brief(
        industry=row.industry,
        task_summary=row.task_summary,
        constraints=list(row.constraints),
        owner_answers=[OwnerAnswer.model_validate(item) for item in row.owner_answers],
        target_maturity=MaturityStage(row.target_maturity) if row.target_maturity else None,
        geography=row.geography,
        budget_hint=row.budget_hint,
        timeline_hint=row.timeline_hint,
        keywords_ru=list(row.keywords_ru),
        keywords_en=list(row.keywords_en),
    )
