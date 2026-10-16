"""Фоновый прогон запроса, пришедшего через `/api/v1` (SPEC Э6-5, РЕШ-250).

Прогон идёт без вопросов — так же, как `scout run --no-interactive`: разбор
текста (`intake_flow.run_intake_stage`), черновик брифа без уточняющего
диалога, дозаполнение ограничений (`resolve_constraints_stage`), затем
`ScoutPipeline` через тот же `web.routes.run._run_pipeline`, что запускает
прогоны веба. Регистрация задачи — в том же реестре `_active_runs` под тем же
системным замком (РЕШ-147): прогон из API и прогон из интерфейса не идут
одновременно.

Стоп-кран `COST_LIMIT_RUB_PER_REQUEST` (РЕШ-45/46) действует на запрос
целиком: роутер пайплайна стартует с суммой, уже потраченной интейком, —
тем же приёмом, что у веба (`initial_cost_rub`, РЕШ-193 п. 1).

Сессию БД задача берёт через `app.db.session_scope` отложенным импортом —
тем же приёмом, что `_run_pipeline` (РЕШ-189 п. 10), чтобы подмена в тестах
доезжала до фоновой задачи.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.config import get_settings
from app.export.markdown import CONSTRAINTS_UNRESOLVED_NOTICE
from app.llm import CostLimitExceededError, LLMRouter
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.pipeline.intake_flow import resolve_constraints_stage, run_intake_stage
from app.pipeline.stages import neutralize_placeholders
from app.schemas import Brief
from app.web.brief_store import save_brief

if TYPE_CHECKING:  # только для аннотаций
    from app.pipeline.persistence import StageRecord

log = logging.getLogger(__name__)

#: Подсказка отрасли, когда приложение её не прислало, — то же умолчание, что
#: у CLI и веба: интейк ведёт себя одинаково на всех каналах.
_INDUSTRY_HINT_DEFAULT = "не указана"


async def create_api_request(task: str, *, client: str) -> int:
    """Завести строку `requests` с владельцем-приложением и закоммитить её.

    Коммит до запуска фоновой задачи обязателен: задача открывает свою сессию
    и должна видеть строку, а не ждать коммита сессии маршрута.
    """
    from app.db import session_scope

    async with session_scope() as session:
        row = RequestRow(raw_text=task, status=RequestStatus.INTAKE, api_client=client)
        session.add(row)
        await session.flush()
        return row.id


async def _mark_failed(request_id: int, error: str, *, cost_rub: float | None = None) -> None:
    """Пометить запрос отказом: статус `failed`, причина, потраченное."""
    from app.db import session_scope

    async with session_scope() as session:
        row = await session.get(RequestRow, request_id)
        if row is None:
            return
        row.status = RequestStatus.FAILED
        row.error = error
        if cost_rub is not None:
            row.cost_rub = cost_rub


async def _prepare_brief(
    request_id: int, task: str, *, industry: str | None, ecosystem: dict[str, Any]
) -> tuple[Brief, dict[str, object], float] | None:
    """Интейк без вопросов: бриф, аргументы пайплайна и уже потраченное.

    `None` — сработал стоп-кран, запрос уже помечен `failed`. Поля экосистемы
    накладываются на бриф ПОСЛЕ интейка: модель их не заполняет (РЕШ-250).
    """
    from app.db import session_scope

    router = LLMRouter(get_settings())
    stage_log: list[StageRecord] = []
    hint = industry or _INDUSTRY_HINT_DEFAULT
    try:
        outcome = await run_intake_stage(router, task, industry_hint=hint, session_factory=None, stage_log=stage_log)
        brief = outcome.brief if outcome.intake is None else outcome.intake.draft_brief
        caveats = list(outcome.caveats)
        brief, unresolved = await resolve_constraints_stage(
            router, brief, task, industry_hint=hint, session_factory=None, stage_log=stage_log
        )
    except CostLimitExceededError as exc:
        await _mark_failed(request_id, str(exc), cost_rub=router.cost.spent_rub)
        return None

    if unresolved:
        caveats.append(CONSTRAINTS_UNRESOLVED_NOTICE)
    if industry:
        brief = brief.model_copy(update={"industry": industry})
    brief = brief.model_copy(update=ecosystem)

    spent = router.cost.spent_rub
    async with session_scope() as session:
        row = await session.get(RequestRow, request_id)
        row.status = RequestStatus.SEARCHING
        row.cost_rub = spent
        await save_brief(session, request_id, brief, confirmed=False)

    # `raw_text` в пайплайн не передаётся — как и у веба: без него накопительный
    # поиск (РЕШ-163) не подтягивает пул чужого запроса с тем же текстом, и
    # приложение не получает выдачу, собранную для другого приложения.
    kwargs: dict[str, object] = {
        "initial_stage_log": stage_log,
        "initial_caveats": caveats,
        "constraints_unresolved": unresolved,
    }
    return brief, kwargs, spent


async def run_api_request(request_id: int, task: str, *, industry: str | None, ecosystem: dict[str, Any]) -> None:
    """Тело фоновой задачи прогона из API: интейк, пайплайн, снятие замка.

    Замок (`_active_runs`) снимается в `finally` при любом исходе. Отказ
    пайплайна `_run_pipeline` разбирает сам (статус `failed` пишет
    `ScoutPipeline.run`); здесь ловится отказ до пайплайна.
    """
    from app.web.routes import run as web_run

    clean_industry = neutralize_placeholders(industry.strip()) if industry and industry.strip() else None
    try:
        prepared = await _prepare_brief(request_id, task, industry=clean_industry, ecosystem=ecosystem)
        if prepared is None:
            return
        brief, kwargs, spent = prepared
        await web_run._run_pipeline(request_id, brief, initial_cost_rub=spent, pipeline_kwargs=kwargs)
    except Exception as exc:
        log.exception("прогон запроса №%s из API завершился отказом до пайплайна", request_id)
        await _mark_failed(request_id, str(exc))
    finally:
        web_run._active_runs.pop(request_id, None)
