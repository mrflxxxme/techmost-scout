"""Страховка ограничений задачи (РЕШ-115, РЕШ-116, РЕШ-122).

Вводит инвариант подэтапа Э2-В: непустой текст задачи с признаками измеримых
требований ⇒ непустой `Brief.constraints` **либо** явная строка клиенту.
Молчаливого третьего исхода нет — сегодня он есть, и именно он провалил ворота
Э2-Б: интейк уложил «точность 95–98%, TRL 6–9» в `task_summary`, `constraints`
остался пуст, и блок «Сверка с требованиями задачи» исчез из всех шести
карточек отчёта (`.planning/evidence/E2B-gate-фарма.md:9`).

По РЕШ-109 «переписать промпт» не может быть критерием приёмки в одиночку —
тот же промпт на том же тексте в том же режиме однажды сработал и однажды
сбоил. Здесь — код-страховка: детектор признаков, один адресный повторный
вызов интейка и страховка блока построчной сверки карточки.

Новый модуль, а не `stages.py`: файл стадий уже на пределе (РЕШ-94, долг), и
класть сюда новую тему — сознательно увеличивать долг.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from app.llm import CostLimitExceededError, LLMRouter, Message
from app.pipeline.persistence import StageRecord
from app.schemas import Brief, IntakeResult, RequirementCheck, SolutionCard

log = logging.getLogger(__name__)

#: Имя стадии повторного адресного вызова в журнале `pipeline_logs` — своё, а
#: не `intake`: иначе 0,1-0,3 руб этого вызова неотличимы от расхода первого
#: разбора текста (постановка задачи, п. 3).
STAGE_INTAKE_CONSTRAINTS = "intake_constraints"

#: Адресное пользовательское сообщение повторного вызова - дословно по РЕШ-115.
CONSTRAINTS_RETRY_MESSAGE = "Выдели ограничения и требуемые показатели."

#: Маркеры измеримых требований - дословно по РЕШ-115. Регистр не важен,
#: сравнение идёт по тексту, приведённому к нижнему регистру.
_LEVEL_MARKERS: tuple[str, ...] = ("не менее", "не более", "trl", "гост")

#: Число с единицей измерения: цифры (с необязательной дробной частью), за
#: которыми следует короткое буквенное обозначение единицы - миллиметры,
#: килограммы, градусы, годы службы. Единица нарочно короткая (1-8 знаков):
#: длиннее - это уже не единица, а случайное слово после числа («12 циклов
#: испытаний» ловить не нужно), и раздувать признак до любого числа в тексте
#: нельзя, иначе детектор ловит вообще всё подряд.
_NUMBER_WITH_UNIT = re.compile(r"\d+(?:[.,]\d+)?\s*°?[a-zA-Zа-яА-Я]{1,8}\b")


def has_requirement_signs(text: str) -> bool:
    """Признаки измеримых требований в тексте задачи (РЕШ-115).

    Число с единицей измерения, «%», «не менее», «не более», «TRL», «ГОСТ».
    Регистр не важен, «%» ищется как символ.

    Эвристика нарочно смещена к ложным срабатываниям, а не к пропуску: лишний
    дешёвый повторный вызов интейка (0,1-0,3 руб) стоит дешевле потерянного
    требования - именно эта потеря провалила ворота Э2-Б. Детерминированное
    извлечение регулярными выражениями отвергнуто (РЕШ-115): функция только
    опознаёт признак, но ничего не извлекает в клиентский текст.
    """
    if "%" in text:
        return True
    lowered = text.lower()
    if any(marker in lowered for marker in _LEVEL_MARKERS):
        return True
    return bool(_NUMBER_WITH_UNIT.search(text))


def _stage_record(
    *, started_at: datetime, cost_before: dict[str, float], router: LLMRouter, error: BaseException | None
) -> StageRecord:
    """Строка журнала повторного вызова - тем же способом, что `cli._intake_stage_record`.

    Отдельная функция, а не переиспользование `cli._intake_stage_record`: та
    всегда требует `error`, а здесь нужна и успешная запись - иначе стоимость
    удавшегося повторного вызова осталась бы неучтённой ни в одной строке
    журнала. Импорт `cost_delta` - отложенный: `app.pipeline.orchestrator`
    импортирует `app.pipeline.stages`, а тот на уровне модуля импортирует этот
    модуль (`ensure_requirement_checks`) - импорт на верхнем уровне здесь
    замкнул бы модули в цикл.
    """
    from app.pipeline.orchestrator import cost_delta

    spent, provider = cost_delta(cost_before, dict(router.cost.by_provider))
    return StageRecord(
        stage=STAGE_INTAKE_CONSTRAINTS,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        provider=provider,
        cost_rub=spent,
        error=str(error) if error is not None else None,
    )


async def retry_constraints_if_missing(
    router: LLMRouter,
    brief: Brief,
    raw_text: str,
    *,
    industry_hint: str,
    stage_log: list[StageRecord],
) -> tuple[Brief, bool]:
    """Дозаполнить `brief.constraints` одним адресным вызовом интейка.

    Вызывается ровно один раз за прогон и только когда `brief.constraints`
    пуст, а `has_requirement_signs(raw_text)` истинно - иначе бриф
    возвращается как есть, второе значение `False`.

    Из ответа берётся **только** `draft_brief.constraints`: второй вызов не
    имеет права переписать `task_summary`, ключевые слова и отрасль - он
    вызван за одним полем (`model_copy` с точечным `update`).

    Возвращает `(бриф, constraints_unresolved)`. Второе значение - `True`,
    если после этого вызова требования всё равно не собраны (отказ модели,
    исключение или пустой ответ) - клиент должен увидеть явную строку
    (РЕШ-115): молчаливого третьего исхода быть не должно.

    `CostLimitExceededError` пробрасывается наружу как есть (РЕШ-45) - это
    команда «стоп тратить», не отказ модели; перед броском строка журнала уже
    дописана в `stage_log`, чтобы трата не потерялась даже при падении
    прогона. Любой другой отказ - предупреждение в лог и продолжение
    (инвариант №5): повторный вызов - доводка, а не обязательный шаг, без
    которого прогон не имеет права дойти до отчёта.
    """
    if brief.constraints or not has_requirement_signs(raw_text):
        return brief, False

    # Отложенный импорт по той же причине, что и в `_stage_record`:
    # `render_prompt` живёт в `stages.py`, а тот на уровне модуля импортирует
    # этот модуль - импорт здесь и только здесь развязывает цикл.
    from app.pipeline.stages import render_prompt

    started = datetime.now(UTC)
    cost_before = dict(router.cost.by_provider)
    try:
        result = await router.complete_json(
            "intake",
            [
                Message(
                    role="system",
                    content=render_prompt("intake", RAW_TASK_TEXT=raw_text, INDUSTRY_HINT=industry_hint),
                ),
                Message(role="user", content=CONSTRAINTS_RETRY_MESSAGE),
            ],
            IntakeResult,
            # Тот же расчёт, что у `run_intake`: рассуждающая модель тратит
            # токены на `reasoning_content` до самого ответа (параграф 0.1 SPEC, п. 4).
            max_tokens=8000,
        )
    except CostLimitExceededError as exc:
        stage_log.append(_stage_record(started_at=started, cost_before=cost_before, router=router, error=exc))
        raise
    except Exception as exc:  # инвариант №5: отказ не роняет прогон
        log.warning("повторный вызов интейка за ограничениями не удался", exc_info=True)
        stage_log.append(_stage_record(started_at=started, cost_before=cost_before, router=router, error=exc))
        return brief, True

    stage_log.append(_stage_record(started_at=started, cost_before=cost_before, router=router, error=None))
    new_constraints = list(dict.fromkeys(item.strip() for item in result.draft_brief.constraints if item.strip()))
    if not new_constraints:
        return brief, True
    return brief.model_copy(update={"constraints": new_constraints}), False


def ensure_requirement_checks(card: SolutionCard, brief: Brief) -> SolutionCard:
    """Страховка блока сверки (РЕШ-122).

    Если `brief.constraints` непуст, а модель вернула пустой
    `requirement_checks`, код дозаполняет список - по одному пункту на
    требование со статусом `not_confirmed`.

    Новых утверждений о факте это не вводит (инвариант №1): статус
    `not_confirmed` - признание отсутствия подтверждения, а не факт. Строка
    лога печатается при каждом срабатывании: страховка маскирует дефект
    промпта `card_author` (правило 18 требует построчную сверку сама), и этот
    риск называется прямо, а не прячется - частота срабатывания должна быть
    видна, а не раствориться в «блок сверки есть у всех карточек».
    """
    if not brief.constraints or card.requirement_checks:
        return card
    log.info(
        "страховка requirement_checks сработала: карточка «%s», требований %d - модель вернула пустой список",
        card.title,
        len(brief.constraints),
    )
    card.requirement_checks = [
        RequirementCheck(requirement=requirement, status="not_confirmed") for requirement in brief.constraints
    ]
    return card
