"""Сценарий интейка, общий для CLI и веба (РЕШ-184).

До этой правки весь сценарий (~250 строк) жил внутри `cli_run._run` и был
недоступен вебу: интерфейс запроса (Э3, И-9) собирает бриф ровно тем же
путём — разбор текста, уточняющий диалог, дозаполнение ограничений, — и
дублировать логику отказов означало бы разные оговорки клиенту и разные
следы трат стоп-крана на двух каналах.

Три стадии вынесены отдельными функциями, а не одной: между стадией разбора
текста (`run_intake_stage`) и стадией уточнения (`refine_brief_stage`)
вызывающая сторона обязана задать вопросы оператору или показать форму —
это единственное место сценария, которое различается между CLI и вебом.
Модуль поэтому **не печатает в консоль и не спрашивает оператора**: вопросы
задаёт вызывающий (CLI — через `typer.prompt`, веб — формой), сюда приходят
уже готовые ответы, а отсюда наружу — бриф, клиентские оговорки и журнал
стадий. `warning` в возвращаемых значениях — предупреждение для лога и
консоли/баннера, а не собственно печать: печатает и логирует по-своему уже
вызывающая сторона.

`CostLimitExceededError` (РЕШ-45) — команда «стоп тратить», не отказ модели,
и не смягчается ни на одной из трёх стадий: исключение пробрасывается
наружу как есть, но перед этим прогон записывается строкой `failed` через
`_persist_intake_stop` — деньги на неудавшийся вызов уже потрачены, и трата
не должна пропасть без следа даже при обрыве прогона до пайплайна.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.llm import CostLimitExceededError
from app.models import RequestStatus
from app.schemas import Brief, IntakeResult, OwnerAnswer

if TYPE_CHECKING:  # только для аннотаций, см. докстроку app.cli_run про отложенный импорт
    from app.llm import LLMRouter
    from app.pipeline.persistence import SessionFactory, StageRecord

log = logging.getLogger(__name__)


#: Оговорка клиенту, когда уточняющий диалог не состоялся и бриф собран кодом
#: из текста задачи (РЕШ-58, П-1 аудита). Отказ интейка виден оператору в
#: консоли, в логе и в `pipeline_logs`, но до эксперта и клиента доживает
#: только отчёт — и он об этом молчал, хотя обеднённый бриф объясняет и
#: широкую выдачу, и пустую сверку с требованиями в карточках.
INTAKE_FALLBACK_CAVEAT = (
    "Уточнения по задаче не собраны: бриф составлен из текста задачи. "
    "Выдача может быть шире и менее точной, чем при уточнённой постановке."
)

#: Оговорка клиенту, когда уточняющий диалог состоялся, а свести ответы в
#: постановку не удалось (доводка П-1). Своя формулировка, а не
#: `INTAKE_FALLBACK_CAVEAT`: тот говорит «бриф составлен из текста задачи», а
#: здесь бриф взят из черновика интейка — было бы неправдой. О качестве выдачи
#: не заявляется ничего: ответы владельца доезжают до модели через
#: `owner_answers`, и утверждать, что выдача от этого хуже, нечем.
REFINE_FALLBACK_CAVEAT = (
    "Постановка задачи осталась черновой: ответы на уточняющие вопросы получены "
    "и учтены при подборе, но в постановку не сведены."
)

#: Имена стадий интейка в журнале `pipeline_logs`. Разведены (совет С-2
#: аудита): отказ разбора текста и отказ уточнения брифа — разные инциденты с
#: разной починкой, а одинаковая строка `intake` их не различала. Маршрут
#: модели у обеих стадий один и тот же — `intake` (РЕШ-46), меняются только
#: имена в журнале.
STAGE_INTAKE = "intake"
STAGE_INTAKE_REFINE = "intake_refine"


def _intake_stage_record(
    stage: str,
    *,
    started_at: datetime,
    cost_before: dict[str, float],
    router: LLMRouter,
    error: BaseException | None = None,
) -> StageRecord:
    """Строка журнала стадии интейка — со снимком счётчика.

    Интейк выполняется до пайплайна, поэтому снимок счётчика вокруг вызова
    снимается здесь — тем же способом и той же функцией, что и в
    `orchestrator._run_stages` (П-4 аудита). Стадия платная (`intake:deepseek`
    в других конфигурациях маршрутов, РЕШ-46), и её расход — что на отказе,
    что на успехе — обязан попасть в журнал.

    `error=None` — стадия завершилась успешно, запись всё равно нужна: иначе
    построчный расход по журналу не сходится с фактической стоимостью прогона.
    """
    from app.pipeline.orchestrator import cost_delta
    from app.pipeline.persistence import StageRecord

    spent, provider = cost_delta(cost_before, dict(router.cost.by_provider))
    return StageRecord(
        stage=stage,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        provider=provider,
        cost_rub=spent,
        error=str(error) if error is not None else None,
    )


async def _persist_intake_stop(
    session_factory: SessionFactory | None,
    *,
    raw_text: str,
    router: LLMRouter,
    stage_log: list[StageRecord],
    record: StageRecord | None,
    error: BaseException,
) -> None:
    """Записать прогон, оборванный стоп-краном (РЕШ-45) на стадии интейка.

    Стоп-кран не смягчается: исключение уходит наружу, прогон обязан упасть.
    Но след обязан остаться (П-5 аудита) — деньги на неудавшийся вызов уже
    потрачены, а до пайплайна прогон не дошёл, значит `persist_run` иначе не
    вызовет никто: `requests` не получал даже строки `failed`, и трата
    исчезала.

    `record=None` — вызывающий уже дописал свою строку в `stage_log` сам (так
    делает `retry_constraints_if_missing`, у которого свой снимок счётчика
    стоимости вокруг вызова): повторный `append` завёл бы дубль записи об
    одном и том же отказе (ревью, цикл 1, совет 2).
    """
    from app.pipeline.persistence import persist_run

    if record is not None:
        stage_log.append(record)
    await persist_run(
        session_factory,
        raw_text=raw_text,
        status=RequestStatus.FAILED,
        cost_rub=round(router.cost.spent_rub, 4),
        cards=[],
        # Прогон оборвался на интейке — карточек нет ни в одной секции.
        contest_cards=[],
        stages=stage_log,
        error=str(error),
    )


@dataclass(slots=True)
class IntakeStageOutcome:
    """Итог стадии разбора текста задачи (`run_intake`).

    `intake` заполнен, когда разбор удался, — вызывающая сторона решает по
    нему, показывать ли уточняющий диалог. `brief` заполнен, только когда
    разбора не было вовсе (отказ) — это уже готовый запасной бриф, диалог
    проводить не из чего. `warning` — текст предупреждения для лога/консоли
    вызывающей стороны, `None` на успехе.
    """

    intake: IntakeResult | None
    brief: Brief | None
    caveats: list[str]
    warning: str | None


async def run_intake_stage(
    router: LLMRouter,
    task: str,
    *,
    industry_hint: str,
    session_factory: SessionFactory | None,
    stage_log: list[StageRecord],
    solution_kind: str = "research",
) -> IntakeStageOutcome:
    """Разобрать текст задачи через `run_intake` с запасным брифом на отказе.

    Устойчивость интейка (Т-2): раньше отказ `run_intake` убивал прогон
    целиком ДО пайплайна и не оставлял в БД даже строки `failed` (аудит Ф-10).
    Теперь отказ — предупреждение (текстом в `warning`, печать — на
    вызывающей стороне) и честный след в `stage_log`, а прогон продолжается
    до отчёта с брифом, собранным кодом из сырого текста. Исчерпание лимита
    стоимости (РЕШ-45) — исключение: команда «стоп тратить», не отказ модели,
    и она пробрасывается наружу как есть.
    """
    from app.pipeline.intake import resolve_solution_kind
    from app.pipeline.stages import brief_from_raw_text, run_intake

    started = datetime.now(UTC)
    cost_before = dict(router.cost.by_provider)
    try:
        intake = await run_intake(router, task, industry_hint=industry_hint, solution_kind=solution_kind)
    except CostLimitExceededError as exc:
        await _persist_intake_stop(
            session_factory,
            raw_text=task,
            router=router,
            stage_log=stage_log,
            record=_intake_stage_record(
                STAGE_INTAKE, started_at=started, cost_before=cost_before, router=router, error=exc
            ),
            error=exc,
        )
        raise
    except Exception as exc:
        warning = "интейк не удался, бриф собран из текста задачи без уточнений"
        log.warning(warning, exc_info=True)
        stage_log.append(
            _intake_stage_record(STAGE_INTAKE, started_at=started, cost_before=cost_before, router=router, error=exc)
        )
        return IntakeStageOutcome(
            intake=None,
            # Запасной бриф ответа модели не имеет: при `unknown` вид решения — `product`.
            brief=brief_from_raw_text(task, industry_hint=industry_hint).model_copy(
                update={"solution_kind": resolve_solution_kind(solution_kind, "research")}
            ),
            caveats=[INTAKE_FALLBACK_CAVEAT],
            warning=warning,
        )

    # Успешный интейк — тоже платный вызов, и его расход обязан попасть в
    # журнал (долг 8 аудита Э2-Г), а не остаться только в счётчике роутера.
    stage_log.append(_intake_stage_record(STAGE_INTAKE, started_at=started, cost_before=cost_before, router=router))
    return IntakeStageOutcome(intake=intake, brief=None, caveats=[], warning=None)


@dataclass(slots=True)
class RefineStageOutcome:
    """Итог стадии сведения ответов владельца в постановку (`refine_brief`)."""

    brief: Brief
    caveats: list[str]
    warning: str | None


async def refine_brief_stage(
    router: LLMRouter,
    task: str,
    intake: IntakeResult,
    answers: dict[str, str],
    *,
    industry_hint: str,
    session_factory: SessionFactory | None,
    stage_log: list[StageRecord],
    solution_kind: str = "research",
) -> RefineStageOutcome:
    """Свести ответы владельца в постановку одним вызовом `refine_brief`.

    Вызывается только когда `answers` непуст — решение, проводить ли диалог и
    какими ответами он закончился, принимает вызывающая сторона (постановка
    модуля). Отказ `refine_brief` не роняет прогон: ответы владельца не
    теряются, но и не выдаются клиенту за ограничения задачи — у них своё поле
    брифа (РЕШ-58, П-2 аудита), а бриф остаётся черновиком интейка с
    дописанными `owner_answers`.
    """
    from app.pipeline.stages import refine_brief

    started = datetime.now(UTC)
    cost_before = dict(router.cost.by_provider)
    try:
        brief = await refine_brief(router, task, answers, industry_hint=industry_hint, solution_kind=solution_kind)
    except CostLimitExceededError as exc:
        await _persist_intake_stop(
            session_factory,
            raw_text=task,
            router=router,
            stage_log=stage_log,
            record=_intake_stage_record(
                STAGE_INTAKE_REFINE, started_at=started, cost_before=cost_before, router=router, error=exc
            ),
            error=exc,
        )
        raise
    except Exception as exc:
        warning = "уточнение брифа не удалось, использую черновик интейка с вашими ответами"
        log.warning("уточнение брифа не удалось, использую черновик интейка с ответами владельца", exc_info=True)
        stage_log.append(
            _intake_stage_record(
                STAGE_INTAKE_REFINE, started_at=started, cost_before=cost_before, router=router, error=exc
            )
        )
        # constraints черновика не трогаем: пара «вопрос — ответ» печатается
        # клиенту строкой «Ограничения:» и разворачивается в
        # requirement_checks каждой карточки — ограничением задачи она не
        # является.
        fallback_brief = intake.draft_brief.model_copy(
            update={
                "owner_answers": [
                    *intake.draft_brief.owner_answers,
                    *(OwnerAnswer(question=question, answer=answer) for question, answer in answers.items()),
                ]
            }
        )
        return RefineStageOutcome(brief=fallback_brief, caveats=[REFINE_FALLBACK_CAVEAT], warning=warning)

    # Успешное уточнение — тоже платный вызов (долг 8 аудита Э2-Г): без этой
    # записи расход по журналу расходился бы с фактической стоимостью прогона.
    stage_log.append(
        _intake_stage_record(STAGE_INTAKE_REFINE, started_at=started, cost_before=cost_before, router=router)
    )
    return RefineStageOutcome(brief=brief, caveats=[], warning=None)


async def resolve_constraints_stage(
    router: LLMRouter,
    brief: Brief,
    task: str,
    *,
    industry_hint: str,
    session_factory: SessionFactory | None,
    stage_log: list[StageRecord],
) -> tuple[Brief, bool]:
    """Дозаполнить `brief.constraints`, если разбор их не собрал (РЕШ-115).

    Тонкая обёртка над `retry_constraints_if_missing`: сама страховка не знает
    о стоп-кране и о записи упавшего прогона в базу, поэтому здесь тот же
    приём, что и на двух стадиях выше, — `CostLimitExceededError` ловится
    только затем, чтобы дописать след `failed`, и пробрасывается дальше.
    `record=None` при записи следа: строка журнала уже дописана внутри
    `retry_constraints_if_missing` своим снимком счётчика стоимости — повторный
    `append` завёл бы дубль (ревью, цикл 1, совет 2).
    """
    from app.pipeline.constraints_guard import retry_constraints_if_missing

    try:
        return await retry_constraints_if_missing(router, brief, task, industry_hint=industry_hint, stage_log=stage_log)
    except CostLimitExceededError as exc:
        await _persist_intake_stop(
            session_factory,
            raw_text=task,
            router=router,
            stage_log=stage_log,
            record=None,
            error=exc,
        )
        raise
